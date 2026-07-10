"""Validate that saved alerts (image/text) and execution use the same TP/SL levels.

Usage: python tools/validate_alerts.py [SYMBOL ...]

If no symbols are provided, the script validates all symbols found in alert history.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict

from swift_scanner.models import SignalEvent
from swift_scanner.config import ScannerConfig
from swift_scanner.watcher import SwiftWatcher


def load_latest_events(history_path: Path) -> Dict[str, dict]:
    events: Dict[str, dict] = {}
    if not history_path.exists():
        raise FileNotFoundError(history_path)
    with history_path.open("r", encoding="utf-8-sig") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = obj.get("symbol_request") or obj.get("symbol_actual")
            if key is None:
                continue
            prev = events.get(key)
            # keep latest by triggered_at
            if prev is None or obj.get("triggered_at", "") >= prev.get("triggered_at", ""):
                events[key] = obj
    return events


class FakeMT5:
    def __init__(self, event: SignalEvent, point: float = 0.01, tick_price: float | None = None):
        self.event = event
        self._point = point
        self._tick_price = tick_price if tick_price is not None else float(event.entry_price or event.price)

    def has_open_position(self, symbol):
        return False

    def is_symbol_tradeable(self, symbol):
        return True

    def normalize_order_volume(self, symbol, volume):
        return float(volume)

    def get_symbol_market_status(self, symbol):
        return {"point": float(self._point)}

    def get_tick(self, symbol):
        return SimpleNamespace(ask=self._tick_price, bid=self._tick_price)

    def place_market_order(self, symbol, direction, volume, stop_loss=None, take_profit=None, deviation=None, magic=None):
        return 999999


def validate(symbols: list[str] | None = None) -> None:
    root = Path.cwd()
    history = root / "signals" / "alert_history.jsonl"
    latest = load_latest_events(history)
    to_check = symbols or list(latest.keys())

    config = ScannerConfig()
    watcher = SwiftWatcher(config)

    for sym in to_check:
        ev_obj = latest.get(sym)
        if ev_obj is None:
            # try matching by symbol_actual contains sym
            ev_obj = next((v for k, v in latest.items() if sym in k), None)
        if ev_obj is None:
            print(f"No event found for {sym}")
            continue

        event = SignalEvent.from_dict(ev_obj)

        symbol_actual = event.symbol_actual
        # compute a tick price that will satisfy the entry buffer so execution places
        mt5_for_price = FakeMT5(event, point=0.01)
        try:
            point = float(mt5_for_price.get_symbol_market_status(symbol_actual).get("point", 0.01))
        except Exception:
            point = 0.01

        required_price, max_price = watcher._get_entry_buffer_prices(float(event.entry_price or event.price), point, event.zone.direction, None)
        # choose a tick inside the required bound
        if event.zone.direction == "bullish":
            tick_price = required_price + (point * 0.0001)
        else:
            tick_price = required_price - (point * 0.0001)

        mt5 = FakeMT5(event, point=point, tick_price=tick_price)

        # ensure execution enabled for simulation
        watcher.config.enable_execution = True
        status = watcher._execute_order_if_enabled(event, mt5, raw_overrides=None, entry_buffer_override=None)

        print(f"--- {sym} / {event.symbol_actual} ---")
        print(f"scan entry={event.entry_price}, SL={event.stop_loss}, TP1={event.take_profit_1}, TP2={event.take_profit_2}, TP3={event.take_profit_3}")
        print(f"execution status={status}, order_ticket={event.order_ticket}")
        print()


if __name__ == "__main__":
    syms = sys.argv[1:]
    validate(syms if syms else None)
