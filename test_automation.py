#!/usr/bin/env python3
"""Quick smoke checks for trade automation behavior."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swift_scanner.config import ScannerConfig
from swift_scanner.models import SignalEvent, Zone
from swift_scanner.watcher import SwiftWatcher


def make_event(symbol_actual: str, timeframe: str, direction: str, label: str) -> SignalEvent:
    zone = Zone(
        zone_type="swift_signal",
        direction=direction,
        symbol_request=symbol_actual,
        symbol_actual=symbol_actual,
        created_at=datetime.now(timezone.utc),
        left_index=0,
        right_index=1,
        top=1.0,
        bottom=0.9,
        formed_index=1,
    )
    return SignalEvent(
        symbol_request=symbol_actual,
        symbol_actual=symbol_actual,
        timeframe=timeframe,
        triggered_at=datetime.now(timezone.utc),
        price=1.1,
        zone=zone,
        signal_label=label,
        entry_price=1.1,
        stop_loss=1.095,
        take_profit=1.12,
    )


def make_watcher(enable_execution: bool) -> SwiftWatcher:
    config = ScannerConfig(
        enable_execution=enable_execution,
        output_dir=Path(gettempdir()) / "tradeview_test_smoke",
    )
    watcher = SwiftWatcher(config)
    watcher.notifier = Mock()
    return watcher


def make_tradeable_mt5() -> Mock:
    mock_mt5 = Mock()
    mock_mt5.has_open_position.return_value = False
    mock_mt5.is_symbol_tradeable.return_value = True
    mock_mt5.get_tick.return_value = SimpleNamespace(ask=1.1006, bid=1.1005)
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.place_market_order.return_value = 12345
    return mock_mt5


def test_automation_with_entry_signal() -> None:
    print("\n=== Test 1: Entry Signal Execution ===")
    watcher = make_watcher(enable_execution=True)
    event = make_event("EURUSD", "M15", "bullish", "Main Long Entry")
    mock_mt5 = make_tradeable_mt5()

    with patch("swift_scanner.watcher.save_signal_chart", return_value=Path("mock_image.png")):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)

    if mock_mt5.place_market_order.called and event.order_ticket == 12345:
        print("PASS: Order placed on entry signal")
        print(f"  - Ticket: {event.order_ticket}")
        print(f"  - Symbol: {event.symbol_actual}")
        print(f"  - Direction: {event.zone.direction}")
    else:
        print("FAIL: Order was not placed")


def test_automation_blocks_duplicate_positions() -> None:
    print("\n=== Test 2: Block Duplicate Positions ===")
    watcher = make_watcher(enable_execution=True)
    event = make_event("EURUSD", "M15", "bullish", "Main Long Entry")
    mock_mt5 = make_tradeable_mt5()
    mock_mt5.has_open_position.return_value = True

    with patch("swift_scanner.watcher.save_signal_chart", return_value=Path("mock_image.png")):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)

    if not mock_mt5.place_market_order.called:
        print("PASS: Order blocked when position exists")
    else:
        print("FAIL: Order placed despite existing position")


def test_automation_respects_disabled_flag() -> None:
    print("\n=== Test 3: Respect Disabled Flag ===")
    watcher = make_watcher(enable_execution=False)
    event = make_event("EURUSD", "M15", "bullish", "Main Long Entry")
    mock_mt5 = make_tradeable_mt5()

    with patch("swift_scanner.watcher.save_signal_chart", return_value=Path("mock_image.png")):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)

    if not mock_mt5.place_market_order.called:
        print("PASS: Execution correctly disabled")
    else:
        print("FAIL: Order placed despite execution disabled")


def test_automation_ignores_tp_sl_signals() -> None:
    print("\n=== Test 4: Ignore TP/SL Signals ===")
    watcher = make_watcher(enable_execution=True)
    event = make_event("EURUSD", "M15", "bullish", "Main Long TP1")
    mock_mt5 = make_tradeable_mt5()

    with patch("swift_scanner.watcher.save_signal_chart", return_value=Path("mock_image.png")):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)

    if not mock_mt5.place_market_order.called:
        print("PASS: TP/SL signals correctly ignored")
    else:
        print("FAIL: TP/SL signal placed an order")


if __name__ == "__main__":
    print("=" * 60)
    print("TRADE AUTOMATION TEST SUITE")
    print("=" * 60)

    test_automation_with_entry_signal()
    test_automation_blocks_duplicate_positions()
    test_automation_respects_disabled_flag()
    test_automation_ignores_tp_sl_signals()

    print("\n" + "=" * 60)
    print("AUTOMATION TESTS COMPLETE")
    print("=" * 60)
