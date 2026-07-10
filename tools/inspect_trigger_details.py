from __future__ import annotations

from pathlib import Path
from dataclasses import replace

from swift_scanner.config import ScannerConfig, TimeframeConfig, build_symbol_scan_configs
from swift_scanner.indicator import detect_swift_signals
from swift_scanner.mt5_client import MT5Client
from swift_scanner.watcher import SwiftWatcher


COLUMNS = [
    "time",
    "swift_main_condition",
    "swift_main_entry",
    "swift_main_stop",
    "swift_main_tp1",
    "swift_main_tp2",
    "swift_main_tp3",
    "swift_last_main_direction",
]


def short(x, n=6):
    try:
        return round(float(x), n)
    except Exception:
        return x


if __name__ == '__main__':
    cfg = ScannerConfig(enable_execution=False, output_dir=Path('signals'))
    watcher = SwiftWatcher(cfg)
    tf = TimeframeConfig(name='M15', mt5_code=1)
    with MT5Client(cfg) as mt5:
        matches = mt5.resolve_watchlist()
        for match in matches:
            requested = match.requested
            actual = match.actual or requested
            print('\n---', requested, '->', actual)
            try:
                bars = mt5.fetch_rates(actual, tf)
            except Exception as exc:
                print(' fetch failed:', exc)
                continue
            if bars is None or len(bars) < 6:
                print(' not enough bars:', 0 if bars is None else len(bars))
                continue
            symbol_cfg = build_symbol_scan_configs().get(requested)
            runtime_cfg, raw_overrides, entry_buffer = watcher._build_runtime_config(symbol_cfg)
            working, events, zones = detect_swift_signals(bars.copy().reset_index(drop=True), requested, actual, 'M15', runtime_cfg)
            last = len(working) - 1
            print(' rows=', len(working), 'events=', len(events), 'zones=', len(zones), 'last_index=', last)
            # show last 8 rows of relevant columns
            df = working.copy()
            for col in COLUMNS:
                if col not in df.columns:
                    df[col] = None
            print(df[COLUMNS].tail(8).to_string(index=True))
            # list events near the end
            recent_events = [e for e in events if e.zone.formed_index >= max(0, last-8)]
            print(' recent events (formed_index >= last-8):', len(recent_events))
            for e in recent_events:
                print('  ', e.signal_label, 'idx=', e.zone.formed_index, 'entry_idx=', e.zone.formed_index, 'dir=', e.zone.direction)
            # check if any event was formed on the last bar
            fresh = [e for e in events if e.zone.formed_index == last]
            print(' fresh on last bar:', len(fresh), 'labels=', [e.signal_label for e in fresh])
            # also show active_zones keys
            print(' active_zones count=', len(zones))
            print()
