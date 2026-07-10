from swift_scanner.config import ScannerConfig
from swift_scanner.watcher import SwiftWatcher, PositionTracker
from types import SimpleNamespace
from unittest.mock import Mock

cfg = ScannerConfig()
watcher = SwiftWatcher(cfg)
tracker = PositionTracker(
    ticket=111,
    symbol='EURUSD',
    direction='bullish',
    entry_price=1.10,
    volume=0.10,
    stop_loss=1.05,
    take_profit_1=1.12,
    take_profit_2=1.15,
    take_profit_3=1.18,
    tp1_qty=0.05,
    tp2_qty=0.03,
    tp3_qty=0.02,
)
tracker.tp1_closed = True
tracker.tp2_closed = True
tracker.tp3_closed = False
watcher.open_positions[111] = tracker
mock_mt5 = Mock()
mock_mt5.get_open_positions.return_value = [SimpleNamespace(ticket=111)]
mock_mt5.get_tick.return_value = SimpleNamespace(ask=1.119, bid=1.118)
mock_mt5.close_position_partial.return_value = 999
watcher._monitor_positions(mock_mt5)
print('called', mock_mt5.close_position_partial.call_count)
print('remaining', watcher.open_positions)
