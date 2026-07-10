from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from swift_scanner.config import ScannerConfig
from swift_scanner.watcher import SwiftWatcher, PositionTracker


def market_tick(ask: float, bid: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(ask=ask, bid=ask if bid is None else bid)


def make_tracker(ticket: int, direction: str = "bullish") -> PositionTracker:
    return PositionTracker(
        ticket=ticket,
        symbol="EURUSD",
        direction=direction,
        entry_price=1.10,
        volume=0.10,
        stop_loss=1.09,
        take_profit_1=1.105,
        take_profit_2=1.11,
        take_profit_3=1.12,
        tp1_qty=0.05,
        tp2_qty=0.03,
        tp3_qty=0.02,
        timeframe="M15",
    )


def test_monitor_positions_smoke_closes_near_sl_and_tp(tmp_path):
    cfg = ScannerConfig(output_dir=tmp_path)
    watcher = SwiftWatcher(cfg)
    watcher.config.stop_loss_proximity_exit_enabled = True
    watcher.config.stop_loss_proximity_exit_ratio = 0.25

    # Tracker A: near SL and invalidated by HTF bias
    t1 = make_tracker(901)
    t1.entry_price = 1.1000
    t1.stop_loss = 1.0900
    t1.timeframe = "M15"
    watcher.open_positions[t1.ticket] = t1

    # Tracker B: hits TP2 and should be fully closed
    t2 = make_tracker(902)
    t2.entry_price = 1.1000
    t2.stop_loss = 1.0900
    t2.take_profit_2 = 1.1100
    t2.timeframe = "M15"
    watcher.open_positions[t2.ticket] = t2

    mock_mt5 = Mock()
    mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=901), SimpleNamespace(ticket=902)]
    # First call for ticket 901: price near SL (ask=1.0908) -> should trigger near-SL closure
    # Second call for ticket 902: price above TP2 (ask=1.1105) -> should trigger TP2 closure
    mock_mt5.get_tick.side_effect = [market_tick(ask=1.0908, bid=1.0907), market_tick(ask=1.1105, bid=1.1104)]
    mock_mt5.get_symbol_market_status.return_value = {"point": 0.0001}
    mock_mt5.close_position_partial.return_value = 1000

    # Force HTF bias to be opposite for tracker A
    watcher._get_htf_bias = Mock(return_value="bearish")

    watcher._monitor_positions(mock_mt5)

    # Near-SL tracker should have been closed and removed
    assert 901 not in watcher.open_positions
    # TP2 partial close should have been executed for tracker 902 (tracker may remain)
    assert mock_mt5.close_position_partial.call_count >= 2
    assert 902 in watcher.open_positions
    assert watcher.open_positions[902].tp2_closed is True
