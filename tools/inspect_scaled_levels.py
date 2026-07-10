import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swift_scanner.config import build_symbol_scan_configs

for symbol in ["XAUUSD", "USOIL", "GBPUSD", "EURUSD", "USDJPY", "AUDUSD", "US30", "NASDAQ", "BTCUSD", "ETHUSD"]:
    config = build_symbol_scan_configs()[symbol]
    print(symbol, config.main_sl_unit, config.main_sl_value, config.main_tp1_value, config.main_tp2_value, config.main_tp3_value, config.main_tp1_unit)
