"""
backtest/engine.py

Replays your existing swift_scanner signal logic (scan_swift) against
historical MT5 data and simulates trade outcomes using your current
SL/TP/displacement rules — WITHOUT touching live trading code.

This does not modify swift_scanner in any way; it only imports and reuses
scan_swift() so the backtest reflects your actual live signal logic.

Usage:
    python backtest/engine.py --symbol XAUUSD --timeframe M15 --bars 10000

Requires: MetaTrader5 terminal running and logged in (same as live scanner),
since historical data is pulled from MT5 directly for accuracy.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

# Ensure swift_scanner package is importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swift_scanner.config import ScannerConfig, build_symbol_scan_configs, resolve_symbol_alias, TIMEFRAME_MAP
from swift_scanner.indicator import scan_swift
from swift_scanner.models import SignalEvent


@dataclass
class SimulatedTrade:
    symbol: str
    timeframe: str
    direction: str
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    outcome: str = "open"          # "tp1", "tp2", "tp3", "sl", "open", "timeout"
    pnl_r: float = 0.0             # profit/loss in R-multiples (risk units)
    bars_held: int = 0


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    trades: list[SimulatedTrade] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_r > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl_r <= 0 and t.outcome != "open")

    @property
    def win_rate(self) -> float:
        closed = [t for t in self.trades if t.outcome != "open"]
        if not closed:
            return 0.0
        return (self.wins / len(closed)) * 100.0

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.trades if t.outcome != "open")

    @property
    def avg_r(self) -> float:
        closed = [t for t in self.trades if t.outcome != "open"]
        if not closed:
            return 0.0
        return self.total_r / len(closed)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_r for t in self.trades if t.pnl_r > 0)
        losses = abs(sum(t.pnl_r for t in self.trades if t.pnl_r < 0))
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_r(self) -> float:
        """Max drawdown measured in cumulative R-multiples."""
        closed = [t for t in self.trades if t.outcome != "open"]
        if not closed:
            return 0.0
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in closed:
            cumulative += t.pnl_r
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_dd = max(max_dd, drawdown)
        return max_dd

    def summary(self) -> str:
        return (
            f"\n=== Backtest Summary: {self.symbol} ({self.timeframe}) ===\n"
            f"Total trades:     {self.total_trades}\n"
            f"Wins / Losses:    {self.wins} / {self.losses}\n"
            f"Win rate:         {self.win_rate:.1f}%\n"
            f"Total R:          {self.total_r:+.2f}R\n"
            f"Avg R per trade:  {self.avg_r:+.2f}R\n"
            f"Profit factor:    {self.profit_factor:.2f}\n"
            f"Max drawdown:     {self.max_drawdown_r:.2f}R\n"
        )


def simulate_trade_outcome(
    df: pd.DataFrame,
    entry_index: int,
    event: SignalEvent,
    max_bars_forward: int = 200,
) -> SimulatedTrade:
    """Walk forward bar-by-bar from entry_index to determine whether SL, TP1, TP2, or TP3 was hit first.

    Uses high/low of each subsequent bar to detect level touches — standard backtesting approach.
    Assumes TP1 closes 50% (or NASDAQ 60%), TP2 closes the remainder, matching watcher.py logic.
    """
    direction = event.zone.direction
    entry_price = float(event.entry_price or event.price)
    sl = float(event.stop_loss)
    tp1 = float(event.take_profit_1)
    tp2 = float(event.take_profit_2)
    tp3 = float(event.take_profit_3)

    trade = SimulatedTrade(
        symbol=event.symbol_actual,
        timeframe=event.timeframe,
        direction=direction,
        entry_time=event.triggered_at,
        entry_price=entry_price,
        stop_loss=sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp3,
    )

    risk_distance = abs(entry_price - sl)
    if risk_distance <= 0:
        trade.outcome = "invalid"
        return trade

    tp1_hit = False
    tp2_hit = False
    realized_r = 0.0

    end_index = min(entry_index + max_bars_forward, len(df) - 1)
    for i in range(entry_index + 1, end_index + 1):
        bar = df.iloc[i]
        high, low = float(bar["high"]), float(bar["low"])
        trade.bars_held = i - entry_index

        if direction == "bullish":
            # Check SL first (conservative — assume worst-case if both hit same bar)
            sl_check = sl if not tp1_hit else entry_price  # breakeven after TP1
            if low <= sl_check:
                if tp1_hit:
                    # stopped at breakeven after partial TP1 — net small positive
                    realized_r += 0.0  # remaining 50% closed at breakeven
                    trade.outcome = "tp1_be"
                else:
                    realized_r -= 1.0
                    trade.outcome = "sl"
                trade.exit_time = bar["time"]
                trade.exit_price = sl_check
                trade.pnl_r = realized_r
                return trade
            if not tp1_hit and high >= tp1:
                tp1_hit = True
                realized_r += 0.5 * ((tp1 - entry_price) / risk_distance)
                trade.outcome = "tp1"
            if tp1_hit and not tp2_hit and high >= tp2:
                tp2_hit = True
                realized_r += 0.3 * ((tp2 - entry_price) / risk_distance)
                trade.outcome = "tp2"
            if tp2_hit and high >= tp3:
                realized_r += 0.2 * ((tp3 - entry_price) / risk_distance)
                trade.outcome = "tp3"
                trade.exit_time = bar["time"]
                trade.exit_price = tp3
                trade.pnl_r = realized_r
                return trade
        else:  # bearish
            sl_check = sl if not tp1_hit else entry_price
            if high >= sl_check:
                if tp1_hit:
                    realized_r += 0.0
                    trade.outcome = "tp1_be"
                else:
                    realized_r -= 1.0
                    trade.outcome = "sl"
                trade.exit_time = bar["time"]
                trade.exit_price = sl_check
                trade.pnl_r = realized_r
                return trade
            if not tp1_hit and low <= tp1:
                tp1_hit = True
                realized_r += 0.5 * ((entry_price - tp1) / risk_distance)
                trade.outcome = "tp1"
            if tp1_hit and not tp2_hit and low <= tp2:
                tp2_hit = True
                realized_r += 0.3 * ((entry_price - tp2) / risk_distance)
                trade.outcome = "tp2"
            if tp2_hit and low <= tp3:
                realized_r += 0.2 * ((entry_price - tp3) / risk_distance)
                trade.outcome = "tp3"
                trade.exit_time = bar["time"]
                trade.exit_price = tp3
                trade.pnl_r = realized_r
                return trade

    # Ran out of bars before resolution
    trade.outcome = "timeout" if not tp1_hit else "tp1_open"
    trade.pnl_r = realized_r
    return trade


def run_backtest(
    symbol_request: str,
    timeframe_name: str,
    bars: int = 5000,
    config: ScannerConfig | None = None,
) -> BacktestResult:
    """Fetch historical data from MT5, replay scan_swift bar-by-bar, simulate outcomes."""
    import MetaTrader5 as mt5

    if config is None:
        config = ScannerConfig()

    if not mt5.initialize():
        raise RuntimeError("MT5 initialize() failed — ensure terminal is running and logged in.")

    try:
        symbol_info = mt5.symbol_info(symbol_request)
        symbol_actual = symbol_request
        if symbol_info is None:
            # try resolving via aliases
            from swift_scanner.config import SYMBOL_ALIASES
            for canonical, aliases in SYMBOL_ALIASES.items():
                if symbol_request.upper() in [a.upper() for a in aliases] or symbol_request.upper() == canonical:
                    for alias in [canonical] + aliases:
                        if mt5.symbol_info(alias) is not None:
                            symbol_actual = alias
                            break
                    break

        tf_code = TIMEFRAME_MAP.get(timeframe_name.upper())
        if tf_code is None:
            raise ValueError(f"Unknown timeframe: {timeframe_name}")

        rates = mt5.copy_rates_from_pos(symbol_actual, tf_code, 0, bars)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No historical data for {symbol_actual} on {timeframe_name}")

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["volume"] = df["tick_volume"].astype(float)
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()

    finally:
        mt5.shutdown()

    result = BacktestResult(symbol=symbol_actual, timeframe=timeframe_name)

    # Walk forward through the data, feeding progressively larger windows to scan_swift
    # to simulate how the live scanner would have seen the data bar-by-bar.
    min_window = 250  # enough lookback for indicators (ATR period, trend period, etc.)
    seen_event_keys: set[str] = set()

    for end_idx in range(min_window, len(df)):
        window = df.iloc[: end_idx + 1].reset_index(drop=True)
        scan_result = scan_swift(window, symbol_request, symbol_actual, timeframe_name, config)

        for event in scan_result.fresh_events:
            event_key = f"{event.triggered_at.isoformat()}|{event.zone.direction}|{event.price}"
            if event_key in seen_event_keys:
                continue
            seen_event_keys.add(event_key)

            if not (event.take_profit_1 and event.take_profit_2 and event.take_profit_3 and event.stop_loss):
                continue

            trade = simulate_trade_outcome(df, end_idx, event)
            result.trades.append(trade)

    return result


def main():
    parser = argparse.ArgumentParser(description="Backtest swift_scanner signals against historical MT5 data.")
    parser.add_argument("--symbol", required=True, help="Symbol to test, e.g. XAUUSD")
    parser.add_argument("--timeframe", default="M15", help="Timeframe, e.g. M15, H1")
    parser.add_argument("--bars", type=int, default=5000, help="Number of historical bars to fetch")
    args = parser.parse_args()

    config = ScannerConfig()
    result = run_backtest(args.symbol, args.timeframe, args.bars, config)
    print(result.summary())

    # Save trade log to CSV for further analysis (performance.py reads this)
    output_dir = Path(__file__).resolve().parent / "results"
    output_dir.mkdir(exist_ok=True)
    out_path = output_dir / f"{args.symbol}_{args.timeframe}_trades.csv"

    rows = [
        {
            "symbol": t.symbol,
            "timeframe": t.timeframe,
            "direction": t.direction,
            "entry_time": t.entry_time,
            "entry_price": t.entry_price,
            "stop_loss": t.stop_loss,
            "tp1": t.take_profit_1,
            "tp2": t.take_profit_2,
            "tp3": t.take_profit_3,
            "exit_time": t.exit_time,
            "exit_price": t.exit_price,
            "outcome": t.outcome,
            "pnl_r": t.pnl_r,
            "bars_held": t.bars_held,
        }
        for t in result.trades
    ]
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"Trade log saved to: {out_path}")


if __name__ == "__main__":
    main()
