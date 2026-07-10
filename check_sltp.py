from swift_scanner.config import build_symbol_scan_configs

configs = build_symbol_scan_configs()

print("=== Symbol Scan Configuration ===")
print("Risk settings are applied to SWIFT alerts and trade execution.")
print()
for symbol, cfg in configs.items():
    print(f"{symbol}:")
    print(f"  Timeframes: {[tf.name for tf in cfg.timeframes]}")
    print(f"  Trend Filter: {cfg.use_trend_filter}")
    print(f"  Scan Interval: {cfg.scan_interval_seconds}s")
    if cfg.main_sl_value is not None:
        print(
            "  Risk: "
            f"SL={cfg.main_sl_value} {cfg.main_sl_unit}, "
            f"TP1={cfg.main_tp1_value} {cfg.main_tp1_unit}, "
            f"TP2={cfg.main_tp2_value} {cfg.main_tp2_unit}, "
            f"TP3={cfg.main_tp3_value} {cfg.main_tp3_unit}"
        )
    if cfg.entry_buffer_min is not None:
        print(f"  Entry Buffer: {cfg.entry_buffer_min}-{cfg.entry_buffer_max} {cfg.entry_buffer_min_unit}")
    print()
