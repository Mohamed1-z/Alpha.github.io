from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swift_scanner.config import build_symbol_scan_configs

OUT = ROOT / "signals" / "per_symbol_risk_table.csv"
OUT.parent.mkdir(parents=True, exist_ok=True)

headers = [
    "symbol",
    "timeframes",
    "scan_interval_seconds",
    "use_trend_filter",
    "main_sl_value",
    "main_sl_unit",
    "main_tp1_value",
    "main_tp1_unit",
    "main_tp2_value",
    "main_tp2_unit",
    "main_tp3_value",
    "main_tp3_unit",
    "entry_buffer_min",
    "entry_buffer_min_unit",
    "entry_buffer_max",
    "entry_buffer_max_unit",
]

configs = build_symbol_scan_configs()

with OUT.open("w", newline="", encoding="utf-8") as fh:
    writer = csv.writer(fh)
    writer.writerow(headers)
    for sym, cfg in sorted(configs.items()):
        tfs = "|".join(tf.name for tf in cfg.timeframes)
        row = [
            sym,
            tfs,
            cfg.scan_interval_seconds,
            bool(cfg.use_trend_filter),
            cfg.main_sl_value,
            cfg.main_sl_unit,
            cfg.main_tp1_value,
            cfg.main_tp1_unit,
            cfg.main_tp2_value,
            cfg.main_tp2_unit,
            cfg.main_tp3_value,
            cfg.main_tp3_unit,
            cfg.entry_buffer_min,
            cfg.entry_buffer_min_unit,
            cfg.entry_buffer_max,
            cfg.entry_buffer_max_unit,
        ]
        writer.writerow(row)

print(f"Wrote: {OUT}")
