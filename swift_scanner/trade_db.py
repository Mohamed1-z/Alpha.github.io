"""
swift_scanner/trade_db.py

SQLite persistence layer for trade history — replaces/supplements the
JSON file state (alert_state.json, open_positions.json) with a queryable
database. Zero setup required (SQLite ships with Python).

This is additive: your existing JSON state files keep working exactly as
they do now. This module gives you a second, queryable record of every
signal and trade outcome for long-term analysis (e.g. "show me every
GBPUSD loss in the last 30 days" with a one-line SQL query instead of
manually parsing JSON).

Usage:
    from swift_scanner.trade_db import TradeDB
    db = TradeDB()  # creates trades.db in project root if it doesn't exist
    db.record_signal(event)
    db.record_execution(ticket, event, volume)
    db.record_close(ticket, exit_price, outcome, pnl)

    # Later, for analysis:
    db.get_trades_by_symbol("XAUUSD")
    db.get_summary_stats()
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("trades.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    price REAL,
    entry_price REAL,
    stop_loss REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    take_profit_3 REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER UNIQUE NOT NULL,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    volume REAL NOT NULL,
    stop_loss REAL,
    take_profit_1 REAL,
    take_profit_2 REAL,
    take_profit_3 REAL,
    executed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket INTEGER NOT NULL,
    exit_price REAL,
    outcome TEXT,
    pnl REAL,
    pnl_r REAL,
    closed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (ticket) REFERENCES executions(ticket)
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_executions_symbol ON executions(symbol);
CREATE INDEX IF NOT EXISTS idx_closures_ticket ON closures(ticket);
"""


class TradeDB:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def record_signal(self, event) -> int:
        """Insert a detected signal event. Returns the new signal row id."""
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (symbol, timeframe, direction, triggered_at, price,
                    entry_price, stop_loss, take_profit_1, take_profit_2, take_profit_3)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.symbol_actual,
                    event.timeframe,
                    event.zone.direction,
                    event.triggered_at.isoformat() if hasattr(event.triggered_at, "isoformat") else str(event.triggered_at),
                    float(event.price) if event.price else None,
                    float(event.entry_price) if event.entry_price else None,
                    float(event.stop_loss) if event.stop_loss else None,
                    float(event.take_profit_1) if event.take_profit_1 else None,
                    float(event.take_profit_2) if event.take_profit_2 else None,
                    float(event.take_profit_3) if event.take_profit_3 else None,
                ),
            )
            return cur.lastrowid

    def record_execution(self, ticket: int, event, volume: float, signal_id: int | None = None) -> None:
        """Insert a record for an order that was actually placed in MT5."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO executions
                   (ticket, signal_id, symbol, direction, entry_price, volume,
                    stop_loss, take_profit_1, take_profit_2, take_profit_3)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticket,
                    signal_id,
                    event.symbol_actual,
                    event.zone.direction,
                    float(event.entry_price or event.price),
                    float(volume),
                    float(event.stop_loss) if event.stop_loss else None,
                    float(event.take_profit_1) if event.take_profit_1 else None,
                    float(event.take_profit_2) if event.take_profit_2 else None,
                    float(event.take_profit_3) if event.take_profit_3 else None,
                ),
            )

    def record_close(self, ticket: int, exit_price: float, outcome: str, pnl: float, pnl_r: float | None = None) -> None:
        """Insert a record for a position close (full or partial)."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO closures (ticket, exit_price, outcome, pnl, pnl_r)
                   VALUES (?, ?, ?, ?, ?)""",
                (ticket, float(exit_price), outcome, float(pnl), pnl_r),
            )

    def get_trades_by_symbol(self, symbol: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT e.*, c.outcome, c.pnl, c.pnl_r, c.closed_at
                   FROM executions e
                   LEFT JOIN closures c ON e.ticket = c.ticket
                   WHERE e.symbol = ?
                   ORDER BY e.executed_at DESC""",
                (symbol,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_summary_stats(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                       COUNT(*) as total_closed,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as total_pnl,
                       AVG(pnl_r) as avg_pnl_r
                   FROM closures"""
            ).fetchone()
            total = row["total_closed"] or 0
            wins = row["wins"] or 0
            return {
                "total_closed_trades": total,
                "wins": wins,
                "win_rate_pct": round((wins / total * 100), 2) if total else 0.0,
                "total_pnl": round(row["total_pnl"] or 0.0, 2),
                "avg_pnl_r": round(row["avg_pnl_r"] or 0.0, 3),
            }

    def get_daily_pnl(self, days: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT date(closed_at) as day, SUM(pnl) as daily_pnl, COUNT(*) as trades
                   FROM closures
                   WHERE closed_at >= datetime('now', ?)
                   GROUP BY date(closed_at)
                   ORDER BY day DESC""",
                (f"-{days} days",),
            ).fetchall()
            return [dict(r) for r in rows]
