from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from swift_scanner.config import ScannerConfig
from swift_scanner.watcher import SwiftWatcher, PositionTracker


def market_tick(ask: float, bid: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(ask=ask, bid=ask if bid is None else bid)


def make_tracker(ticket: int) -> PositionTracker:
    return PositionTracker(
        ticket=ticket,
        symbol="EURUSD",
        direction="bullish",
        entry_price=1.10,
        volume=0.10,
        stop_loss=1.05,
        take_profit_1=1.12,
        take_profit_2=1.15,
        take_profit_3=1.18,
        tp1_qty=0.05,
        tp2_qty=0.03,
        tp3_qty=0.02,
        timeframe="M15",
    )


def test_reversal_after_tp2_closes_remaining(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    ticket = 111
    tracker = make_tracker(ticket)
    # simulate that TP1 and TP2 have been closed already
    tracker.tp1_closed = True
    tracker.tp2_closed = True
    tracker.tp3_closed = False

    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    # position exists in MT5
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    # current price has reversed back to TP1 or below
    mock_mt5.get_tick.return_value = market_tick(ask=1.119, bid=1.118)
    # closing the remaining should succeed
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    # remaining (tp3_qty) should have been closed and tracker removed
    mock_mt5.close_position_partial.assert_called()
    assert ticket not in watcher.open_positions


def test_reversal_after_tp2_closes_remaining_around_tp2(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    ticket = 112
    tracker = make_tracker(ticket)
    tracker.tp1_closed = True
    tracker.tp2_closed = True
    tracker.tp3_closed = False

    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.1495, bid=1.1490)
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_called_once()
    assert ticket not in watcher.open_positions


def test_reversal_after_tp1_closes_remaining_around_tp1(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    ticket = 113
    tracker = make_tracker(ticket)
    tracker.tp1_closed = True
    tracker.tp2_closed = False
    tracker.tp3_closed = False

    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.1195, bid=1.1190)
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_called_once()
    assert ticket not in watcher.open_positions


def test_progressive_tp_closes_sequence(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    ticket = 222
    tracker = make_tracker(ticket)
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    # Step 1: hit TP1 using executable bid-side price for a long position
    mock_mt5.get_tick.return_value = market_tick(ask=1.1205, bid=1.1200)
    mock_mt5.close_position_partial.return_value = 101
    watcher._monitor_positions(mock_mt5)
    assert tracker.tp1_closed is True
    # Step 2: hit TP2 using executable bid-side price for a long position
    mock_mt5.get_tick.return_value = market_tick(ask=1.1505, bid=1.1500)
    mock_mt5.close_position_partial.return_value = 102
    watcher._monitor_positions(mock_mt5)
    assert tracker.tp2_closed is True
    # Step 3: hit TP3 (final close) using executable bid-side price
    mock_mt5.get_tick.return_value = market_tick(ask=1.1805, bid=1.1800)
    mock_mt5.close_position_partial.return_value = 103
    watcher._monitor_positions(mock_mt5)

    # After final close, tracker should be removed
    assert ticket not in watcher.open_positions


def test_monitor_positions_uses_bid_for_long_positions(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    ticket = 333
    tracker = make_tracker(ticket)
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.1201, bid=1.1199)
    mock_mt5.close_position_partial.return_value = 101

    watcher._monitor_positions(mock_mt5)

    assert tracker.tp1_closed is False
    mock_mt5.close_position_partial.assert_not_called()


def test_near_sl_proximity_closes_invalidated_trade(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 555
    tracker = make_tracker(ticket)
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.0900
    tracker.take_profit_1 = 1.1050
    tracker.take_profit_2 = 1.1100
    tracker.take_profit_3 = 1.1200
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.0908, bid=1.0907)
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.get_recent_candles.return_value = [
        {"open": 1.0850, "high": 1.0910, "low": 1.0840, "close": 1.0905, "volume": 10},
    ] * 20
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_called_once()
    assert ticket not in watcher.open_positions


def test_near_sl_proximity_closes_invalidated_bearish_trade(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 556
    tracker = make_tracker(ticket)
    tracker.direction = "bearish"
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.1100
    tracker.take_profit_1 = 1.0950
    tracker.take_profit_2 = 1.0900
    tracker.take_profit_3 = 1.0850
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.1090, bid=1.1089)
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.get_recent_candles.return_value = [
        {"open": 1.1110, "high": 1.1120, "low": 1.1080, "close": 1.1095, "volume": 10},
    ] * 20
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_called_once()
    assert ticket not in watcher.open_positions


def test_monitor_positions_closes_near_sl_on_opposite_htf_bias(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 560
    tracker = make_tracker(ticket)
    tracker.direction = "bullish"
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.0900
    tracker.take_profit_1 = 1.1050
    tracker.take_profit_2 = 1.1100
    tracker.take_profit_3 = 1.1200
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    tracker.timeframe = "M15"
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.0908, bid=1.0907)
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.close_position_partial.return_value = 999
    watcher._get_htf_bias = Mock(return_value="bearish")

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_called_once_with(
        tracker.symbol,
        tracker.direction,
        tracker.get_remaining_volume(),
        position_ticket=ticket,
        deviation=watcher.config.order_deviation,
        magic=watcher.config.order_magic_number,
    )
    assert ticket not in watcher.open_positions


def test_near_sl_proximity_skips_symbol_not_in_filter(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    cfg.stop_loss_proximity_exit_symbols = ["EURUSD"]
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 557
    tracker = make_tracker(ticket)
    tracker.symbol = "BTCUSDm"
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.0900
    tracker.take_profit_1 = 1.1050
    tracker.take_profit_2 = 1.1100
    tracker.take_profit_3 = 1.1200
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.0908, bid=1.0907)
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.get_recent_candles.return_value = [
        {"open": 1.0850, "high": 1.0910, "low": 1.0840, "close": 1.0905, "volume": 10},
    ] * 20
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_not_called()
    assert ticket in watcher.open_positions


def test_near_sl_proximity_closes_on_opposite_htf_bias(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 558
    tracker = make_tracker(ticket)
    tracker.direction = "bullish"
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.0900
    tracker.take_profit_1 = 1.1050
    tracker.take_profit_2 = 1.1100
    tracker.take_profit_3 = 1.1200
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    tracker.timeframe = "M15"
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    watcher._get_htf_bias = Mock(return_value="bearish")

    is_invalidated = watcher._should_close_near_stop_loss(
        tracker,
        current_price=1.0908,
        mt5_client=mock_mt5,
        point=0.0001,
    )

    assert is_invalidated is True


def test_near_sl_proximity_closes_on_opposite_htf_bias_for_bearish_trade(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 559
    tracker = make_tracker(ticket)
    tracker.direction = "bearish"
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.1100
    tracker.take_profit_1 = 1.0950
    tracker.take_profit_2 = 1.0900
    tracker.take_profit_3 = 1.0850
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    tracker.timeframe = "M15"
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    watcher._get_htf_bias = Mock(return_value="bullish")

    is_invalidated = watcher._should_close_near_stop_loss(
        tracker,
        current_price=1.1080,
        mt5_client=mock_mt5,
        point=0.0001,
    )

    assert is_invalidated is True


def test_near_sl_proximity_skips_unconfigured_timeframe(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    ticket = 558
    tracker = make_tracker(ticket)
    tracker.timeframe = "H1"
    tracker.entry_price = 1.1000
    tracker.stop_loss = 1.0900
    tracker.take_profit_1 = 1.1050
    tracker.take_profit_2 = 1.1100
    tracker.take_profit_3 = 1.1200
    tracker.tp1_qty = 0.03
    tracker.tp2_qty = 0.03
    tracker.tp3_qty = 0.04
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.0908, bid=1.0907)
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.get_recent_candles.return_value = [
        {"open": 1.0850, "high": 1.0910, "low": 1.0840, "close": 1.0905, "volume": 10},
    ] * 20
    mock_mt5.close_position_partial.return_value = 999

    watcher._monitor_positions(mock_mt5)

    mock_mt5.close_position_partial.assert_not_called()
    assert ticket in watcher.open_positions


def test_monitor_positions_uses_ask_for_short_positions(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    ticket = 444
    tracker = make_tracker(ticket)
    tracker.direction = "bearish"
    tracker.take_profit_1 = 1.1000
    tracker.take_profit_2 = 1.0950
    tracker.take_profit_3 = 1.0900
    watcher.open_positions[ticket] = tracker

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=ticket)]
    mock_mt5.get_tick.return_value = market_tick(ask=1.1001, bid=1.0999)
    mock_mt5.close_position_partial.return_value = 102

    watcher._monitor_positions(mock_mt5)

    assert tracker.tp1_closed is False
    mock_mt5.close_position_partial.assert_not_called()
