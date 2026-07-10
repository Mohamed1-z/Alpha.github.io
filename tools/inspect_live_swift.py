from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from swift_scanner.config import ScannerConfig, TimeframeConfig, build_symbol_scan_configs
from swift_scanner.indicator import detect_swift_signals, scan_swift
from swift_scanner.mt5_client import MT5Client
from swift_scanner.watcher import SwiftWatcher


if __name__ == '__main__':
    cfg = ScannerConfig(enable_execution=False, output_dir=Path('signals'))
    watcher = SwiftWatcher(cfg)
    tf = TimeframeConfig(name='M15', mt5_code=1)
    with MT5Client(cfg) as mt5:
        matches = mt5.resolve_watchlist()
        for match in matches:
            actual = match.actual or match.requested
            try:
                bars = mt5.fetch_rates(actual, tf)
            except Exception as exc:
                print(match.requested, '->', actual, 'fetch failed:', exc)
                continue
            if bars is None or len(bars) < 2:
                print(match.requested, '->', actual, 'no bars')
                continue
            symbol_config = build_symbol_scan_configs().get(match.requested)
            runtime_cfg, raw_overrides, entry_buffer = watcher._build_runtime_config(symbol_config)
            bars2, events, zones = detect_swift_signals(bars.copy().reset_index(drop=True), match.requested, actual, 'M15', runtime_cfg)
            print(match.requested, '->', actual, 'rows=', len(bars2), 'events=', len(events), 'zones=', len(zones))
            print('  latest labels=', [(e.signal_label, e.zone.formed_index, e.zone.direction) for e in events[-5:]])
            result = scan_swift(bars.copy().reset_index(drop=True), match.requested, actual, 'M15', runtime_cfg)
            print('  fresh from scan_swift=', len(result.fresh_events), 'labels=', [(e.signal_label, e.zone.formed_index, e.zone.direction) for e in result.fresh_events])
            relaxed_cfg = replace(runtime_cfg, use_trend_filter=False)
            bars3, events3, zones3 = detect_swift_signals(bars.copy().reset_index(drop=True), match.requested, actual, 'M15', relaxed_cfg)
            print('  relaxed events=', len(events3), 'labels=', [(e.signal_label, e.zone.formed_index, e.zone.direction) for e in events3[-5:]])
            relaxed_result = scan_swift(bars.copy().reset_index(drop=True), match.requested, actual, 'M15', relaxed_cfg)
            print('  relaxed fresh=', len(relaxed_result.fresh_events), 'labels=', [(e.signal_label, e.zone.formed_index, e.zone.direction) for e in relaxed_result.fresh_events])
            print()
