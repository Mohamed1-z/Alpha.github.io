from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from swift_scanner.config import ScannerConfig
from swift_scanner.watcher import PositionTracker, SwiftWatcher


def make_watcher(**config_overrides) -> SwiftWatcher:
    config = ScannerConfig(**config_overrides)
    watcher = SwiftWatcher.__new__(SwiftWatcher)
    watcher.config = config
    watcher.open_positions = {}
    watcher.news_flattened_events = set()
    watcher.upcoming_news_events = []
    return watcher


def make_tracker(ticket: int, symbol: str) -> PositionTracker:
    return PositionTracker(
        ticket=ticket,
        symbol=symbol,
        direction="bearish",
        entry_price=58689.79,
        volume=0.1,
        stop_loss=59276.69,
        take_profit_1=57926.82,
        take_profit_2=57222.55,
        take_profit_3=56342.20,
        tp1_qty=0.033,
        tp2_qty=0.033,
        tp3_qty=0.034,
    )


def test_flattens_position_when_event_is_within_window():
    watcher = make_watcher(news_flatten_enabled=True, news_flatten_minutes_before=30)
    watcher.open_positions[123] = make_tracker(123, "BTCUSDm")
    event_time = datetime.now(timezone.utc) + timedelta(minutes=20)
    watcher.upcoming_news_events = [(event_time, ["BTCUSD", "XAUUSD"])]

    mt5_client = Mock()
    mt5_client.close_position_full.return_value = True

    watcher.check_news_flatten(mt5_client)

    mt5_client.close_position_full.assert_called_once_with(123)
    assert 123 not in watcher.open_positions


def test_does_not_flatten_when_event_is_far_away():
    watcher = make_watcher(news_flatten_enabled=True, news_flatten_minutes_before=30)
    watcher.open_positions[123] = make_tracker(123, "BTCUSDm")
    event_time = datetime.now(timezone.utc) + timedelta(hours=4)
    watcher.upcoming_news_events = [(event_time, ["BTCUSD"])]

    mt5_client = Mock()
    watcher.check_news_flatten(mt5_client)

    mt5_client.close_position_full.assert_not_called()
    assert 123 in watcher.open_positions


def test_ignores_events_already_in_the_past():
    watcher = make_watcher(news_flatten_enabled=True, news_flatten_minutes_before=30)
    watcher.open_positions[123] = make_tracker(123, "BTCUSDm")
    event_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    watcher.upcoming_news_events = [(event_time, ["BTCUSD"])]

    mt5_client = Mock()
    watcher.check_news_flatten(mt5_client)

    mt5_client.close_position_full.assert_not_called()
    assert 123 in watcher.open_positions


def test_does_not_flatten_unaffected_symbol():
    watcher = make_watcher(news_flatten_enabled=True, news_flatten_minutes_before=30)
    watcher.open_positions[123] = make_tracker(123, "EURUSDm")
    event_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    watcher.upcoming_news_events = [(event_time, ["BTCUSD"])]  # doesn't affect EURUSD

    mt5_client = Mock()
    watcher.check_news_flatten(mt5_client)

    mt5_client.close_position_full.assert_not_called()
    assert 123 in watcher.open_positions


def test_respects_disabled_flag():
    watcher = make_watcher(news_flatten_enabled=False)
    watcher.open_positions[123] = make_tracker(123, "BTCUSDm")
    event_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    watcher.upcoming_news_events = [(event_time, ["BTCUSD"])]

    mt5_client = Mock()
    watcher.check_news_flatten(mt5_client)

    mt5_client.close_position_full.assert_not_called()
    assert 123 in watcher.open_positions


def test_does_not_double_flatten_same_event():
    watcher = make_watcher(news_flatten_enabled=True, news_flatten_minutes_before=30)
    event_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    watcher.upcoming_news_events = [(event_time, ["BTCUSD"])]

    mt5_client = Mock()
    mt5_client.close_position_full.return_value = True

    # First position gets closed and the event is marked as handled for BTCUSD.
    watcher.open_positions[123] = make_tracker(123, "BTCUSDm")
    watcher.check_news_flatten(mt5_client)
    assert mt5_client.close_position_full.call_count == 1

    # A second BTC position opened afterwards for the *same* event/symbol pair should
    # not be repeatedly targeted once that key is recorded (guards against re-processing
    # loops firing on every scan cycle for a position that failed to close, etc. -- here
    # we simulate the already-recorded-event case directly).
    watcher.open_positions[456] = make_tracker(456, "BTCUSDm")
    canonical = "BTCUSD"
    watcher.news_flattened_events.add((event_time.isoformat(), canonical))
    watcher.check_news_flatten(mt5_client)
    assert mt5_client.close_position_full.call_count == 1
    assert 456 in watcher.open_positions
