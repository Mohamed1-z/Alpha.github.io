#!/usr/bin/env python3
"""Debug script to verify configuration values are being applied correctly."""

from swift_scanner.config import build_symbol_scan_configs, resolve_symbol_alias

# Print all symbol configs to verify they're set correctly
configs = build_symbol_scan_configs()

print("=" * 80)
print("CONFIGURATION VALUES BY SYMBOL")
print("=" * 80)

for symbol, cfg in configs.items():
    print(f"\n{symbol}:")
    print(f"  Timeframes: {[tf.name for tf in cfg.timeframes]}")
    print(f"  Trend Filter: {cfg.use_trend_filter}")
    print(f"  Scan Interval: {cfg.scan_interval_seconds}s")
    if cfg.entry_buffer_min is not None:
        print(f"  Entry Buffer: {cfg.entry_buffer_min} {cfg.entry_buffer_min_unit} to {cfg.entry_buffer_max} {cfg.entry_buffer_max_unit}")

# Test symbol alias resolution
print("\n" + "=" * 80)
print("SYMBOL ALIAS RESOLUTION")
print("=" * 80)

test_symbols = ["USTEC", "NAS100", "US100", "NASDAQ", "US30m", "US30", "BTCUSD", "ETH", "XAUUSD"]
for sym in test_symbols:
    resolved = resolve_symbol_alias(sym)
    print(f"{sym:15} -> {resolved}")
