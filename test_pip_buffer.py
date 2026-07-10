#!/usr/bin/env python3
"""Test script to verify 2-pip entry buffer feature."""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

from swift_scanner.config import ScannerConfig
from swift_scanner.models import SignalEvent, Zone
from swift_scanner.watcher import SwiftWatcher


def make_event(symbol_actual: str, direction: str, entry_price: float) -> SignalEvent:
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
        timeframe="M15",
        triggered_at=datetime.now(timezone.utc),
        price=entry_price,
        entry_price=entry_price,
        zone=zone,
        signal_label="Main Long Entry",
    )


def test_bullish_entry_waits_for_2pip_buffer():
    """Test: Bullish entry should wait for price to move up 2 pips."""
    print("\n=== Test 1: Bullish Entry Waits for 2-Pip Buffer ===")
    config = ScannerConfig(enable_execution=True, entry_pip_buffer=2.0)
    watcher = SwiftWatcher(config)
    
    signal_price = 1.1000
    event = make_event("EURUSD", "bullish", signal_price)
    event.stop_loss = 1.0950
    event.take_profit = 1.1200
    
    mock_mt5 = Mock()
    mock_mt5.has_open_position.return_value = False
    mock_mt5.is_symbol_tradeable.return_value = True
    
    # Price hasn't moved 2 pips yet (only at 1.1001, need 1.1002)
    tick = Mock()
    tick.ask = 1.1001
    tick.bid = 1.0999
    mock_mt5.get_tick.return_value = tick
    
    mock_mt5.get_symbol_market_status.return_value = {
        "symbol": "EURUSD",
        "point": 0.0001,
        "trade_allowed": True,
    }
    
    watcher.notifier = Mock()
    
    with patch("swift_scanner.watcher.save_signal_chart", return_value="mock_image.png"):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)
    
    if not mock_mt5.place_market_order.called:
        print("✓ PASS: Order blocked - price hasn't moved 2 pips yet")
        print(f"  - Signal price: {signal_price:.5f}")
        print(f"  - Current ask: {tick.ask:.5f}")
        print(f"  - Required: {signal_price + 0.0002:.5f}")
    else:
        print("✗ FAIL: Order placed before pip buffer satisfied")


def test_bullish_entry_executes_after_2pip_buffer():
    """Test: Bullish entry should execute after price moves up 2 pips."""
    print("\n=== Test 2: Bullish Entry Executes After 2-Pip Buffer ===")
    config = ScannerConfig(enable_execution=True, entry_pip_buffer=2.0)
    watcher = SwiftWatcher(config)
    
    signal_price = 1.1000
    event = make_event("EURUSD", "bullish", signal_price)
    event.stop_loss = 1.0950
    event.take_profit = 1.1200
    
    mock_mt5 = Mock()
    mock_mt5.has_open_position.return_value = False
    mock_mt5.is_symbol_tradeable.return_value = True
    mock_mt5.place_market_order.return_value = 12345
    
    # Price has moved 2 pips up (at 1.1002)
    tick = Mock()
    tick.ask = 1.1002
    tick.bid = 1.1001
    mock_mt5.get_tick.return_value = tick
    
    mock_mt5.get_symbol_market_status.return_value = {
        "symbol": "EURUSD",
        "point": 0.0001,
        "trade_allowed": True,
    }
    
    watcher.notifier = Mock()
    
    with patch("swift_scanner.watcher.save_signal_chart", return_value="mock_image.png"):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)
    
    if mock_mt5.place_market_order.called:
        print("✓ PASS: Order executed after price moved 2 pips")
        print(f"  - Signal price: {signal_price:.5f}")
        print(f"  - Current ask: {tick.ask:.5f}")
        print(f"  - Buffer satisfied: +2 pips")
    else:
        print("✗ FAIL: Order not placed despite buffer being satisfied")


def test_bearish_entry_waits_for_2pip_buffer():
    """Test: Bearish entry should wait for price to move down 2 pips."""
    print("\n=== Test 3: Bearish Entry Waits for 2-Pip Buffer ===")
    config = ScannerConfig(enable_execution=True, entry_pip_buffer=2.0)
    watcher = SwiftWatcher(config)
    
    signal_price = 1.1000
    event = make_event("EURUSD", "bearish", signal_price)
    event.stop_loss = 1.1050
    event.take_profit = 1.0800
    
    mock_mt5 = Mock()
    mock_mt5.has_open_position.return_value = False
    mock_mt5.is_symbol_tradeable.return_value = True
    
    # Price hasn't moved 2 pips down yet (at 1.0999, need 1.0998)
    tick = Mock()
    tick.ask = 1.1001
    tick.bid = 1.0999
    mock_mt5.get_tick.return_value = tick
    
    mock_mt5.get_symbol_market_status.return_value = {
        "symbol": "EURUSD",
        "point": 0.0001,
        "trade_allowed": True,
    }
    
    watcher.notifier = Mock()
    
    with patch("swift_scanner.watcher.save_signal_chart", return_value="mock_image.png"):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)
    
    if not mock_mt5.place_market_order.called:
        print("✓ PASS: Order blocked - price hasn't moved down 2 pips yet")
        print(f"  - Signal price: {signal_price:.5f}")
        print(f"  - Current bid: {tick.bid:.5f}")
        print(f"  - Required: {signal_price - 0.0002:.5f}")
    else:
        print("✗ FAIL: Order placed before pip buffer satisfied")


def test_custom_pip_buffer():
    """Test: Custom pip buffer value (5 pips instead of 2)."""
    print("\n=== Test 4: Custom Pip Buffer (5 pips) ===")
    config = ScannerConfig(enable_execution=True, entry_pip_buffer=5.0)
    watcher = SwiftWatcher(config)
    
    signal_price = 1.1000
    event = make_event("EURUSD", "bullish", signal_price)
    event.stop_loss = 1.0950
    event.take_profit = 1.1200
    
    mock_mt5 = Mock()
    mock_mt5.has_open_position.return_value = False
    mock_mt5.is_symbol_tradeable.return_value = True
    
    # Price moved only 3 pips (at 1.1003, need 1.1005 for 5-pip buffer)
    tick = Mock()
    tick.ask = 1.1003
    tick.bid = 1.1002
    mock_mt5.get_tick.return_value = tick
    
    mock_mt5.get_symbol_market_status.return_value = {
        "symbol": "EURUSD",
        "point": 0.0001,
        "trade_allowed": True,
    }
    
    watcher.notifier = Mock()
    
    with patch("swift_scanner.watcher.save_signal_chart", return_value="mock_image.png"):
        watcher.handle_event(Mock(fresh_events=[event]), event, mock_mt5)
    
    if not mock_mt5.place_market_order.called:
        print("✓ PASS: Order blocked - only 3 pips moved, need 5")
        print(f"  - Signal price: {signal_price:.5f}")
        print(f"  - Current ask: {tick.ask:.5f}")
        print(f"  - Required: {signal_price + 0.0005:.5f}")
    else:
        print("✗ FAIL: Order placed before 5-pip buffer satisfied")


if __name__ == "__main__":
    print("=" * 60)
    print("2-PIP ENTRY BUFFER TEST SUITE")
    print("=" * 60)
    
    test_bullish_entry_waits_for_2pip_buffer()
    test_bullish_entry_executes_after_2pip_buffer()
    test_bearish_entry_waits_for_2pip_buffer()
    test_custom_pip_buffer()
    
    print("\n" + "=" * 60)
    print("PIP BUFFER TESTS COMPLETE")
    print("=" * 60)
