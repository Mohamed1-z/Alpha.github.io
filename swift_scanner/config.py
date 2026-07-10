from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import MetaTrader5 as mt5


DEFAULT_WATCHLIST = [
    "BTCUSD",
    "ETHUSD",
    "USOIL",
    "XAUUSD",
    "NASDAQ",
    "US30",
    "US500",
    "EURUSD",
    "USDJPY",
    "GBPUSD",
    "AUDUSD",
]

SYMBOL_ALIASES = {
    "BTCUSD": ["BTCUSDm", "BTCUSD.", "XBTUSD", "BTCUSDx"],
    "ETHUSD": ["ETHUSDm", "ETHUSD.", "XETHUSD", "ETHUSDx"],
    "USOIL": ["WTI", "WTIUSD", "XTIUSD", "USOil", "WTIUSDm", "USOILm", "CLUSD"],
    "XAUUSD": ["GOLD", "XAUUSDm", "XAUUSDM", "GOLDm"],
    "NASDAQ": ["NAS100", "USTEC", "USTEC_x100m", "US100", "NAS", "NAS100m", "NAS100_x100m", "US100m"],
    # Prefer x10 broker symbols for US30 because their contract sizing matches the configured risk table.
    "US30": ["US30_x10m", "US30_x10", "US30m", "DJI30", "WS30", "DJ30", "WALLSTREET", "WALLSTREETm"],
    "US500": ["US500_x100m", "SP500", "SPX500", "US500m"],
    "EURUSD": ["EURUSDm", "EURUSD.", "EURUSDx"],
    "USDJPY": ["USDJPYm", "USDJPY.", "USDJPYx"],
    "GBPUSD": ["GBPUSDm", "GBPUSD.", "GBPUSDx"],
    "AUDUSD": ["AUDUSDm", "AUDUSD.", "AUDUSDx"],
}

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

# Keep defaults as M5/M15 for compatibility; M1 is opt-in per-symbol
DEFAULT_TIMEFRAME_NAMES = ["M5", "M15"]


@dataclass(slots=True, frozen=True)
class TimeframeConfig:
    name: str
    mt5_code: int


@dataclass(slots=True, frozen=True)
class TimeframeRiskConfig:
    main_sl_value: float | None = None
    main_sl_unit: str | None = None
    main_tp1_value: float | None = None
    main_tp1_unit: str | None = None
    main_tp2_value: float | None = None
    main_tp2_unit: str | None = None
    main_tp3_value: float | None = None
    main_tp3_unit: str | None = None
    entry_buffer_min: float | None = None
    entry_buffer_min_unit: str | None = None
    entry_buffer_max: float | None = None
    entry_buffer_max_unit: str | None = None


def parse_chat_ids(*raw_values: str | None) -> list[str]:
    chat_ids: list[str] = []
    seen: set[str] = set()

    for raw_value in raw_values:
        if not raw_value:
            continue
        for item in raw_value.replace(";", ",").split(","):
            chat_id = item.strip()
            if not chat_id or chat_id in seen:
                continue
            chat_ids.append(chat_id)
            seen.add(chat_id)

    return chat_ids


def build_timeframes(names: list[str]) -> list[TimeframeConfig]:
    resolved: list[TimeframeConfig] = []
    seen: set[str] = set()

    for name in names:
        normalized = name.upper()
        if normalized in seen:
            continue
        if normalized not in TIMEFRAME_MAP:
            supported = ", ".join(TIMEFRAME_MAP)
            raise ValueError(f"Unsupported timeframe '{name}'. Supported values: {supported}")
        resolved.append(TimeframeConfig(name=normalized, mt5_code=TIMEFRAME_MAP[normalized]))
        seen.add(normalized)

    return resolved


@dataclass(slots=True, frozen=True)
class SymbolScanConfig:
    timeframes: list[TimeframeConfig]
    use_trend_filter: bool | None = None
    scan_interval_seconds: int | None = None
    main_sl_value: float | None = None
    main_sl_unit: str | None = None
    main_tp1_value: float | None = None
    main_tp1_unit: str | None = None
    main_tp2_value: float | None = None
    main_tp2_unit: str | None = None
    main_tp3_value: float | None = None
    main_tp3_unit: str | None = None
    timeframe_risk_configs: dict[str, TimeframeRiskConfig] = field(default_factory=dict)
    # Entry buffer range (min, max) with unit. Unit options: 'percent', 'pips', 'points', 'price'
    entry_buffer_min: float | None = None
    entry_buffer_min_unit: str | None = None
    entry_buffer_max: float | None = None
    entry_buffer_max_unit: str | None = None


def normalize_symbol(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalnum())


def resolve_symbol_alias(symbol: str) -> str | None:
    normalized = normalize_symbol(symbol)
    for canonical, aliases in SYMBOL_ALIASES.items():
        if normalize_symbol(canonical) == normalized:
            return canonical
        for alias in aliases:
            if normalize_symbol(alias) == normalized:
                return canonical
    return None


def scale_tp_levels_from_tp3(
    sl_value: float,
    tp1_value: float,
    tp2_value: float,
    tp3_value: float,
    target_tp3_rr: float,
) -> tuple[float, float, float]:
    """Scale TP1/TP2/TP3 proportionally from SL so TP3 reaches the requested RR target.

    This helper is used to convert legacy TP ratios into the current target TP3 risk-reward
    ratio. For symbols moved from 2.0R to 2.5R, the returned TP1/TP2/TP3 values are scaled
    proportionally so the relative structure is preserved while TP3 reaches the new target.
    """
    old_tp3_rr = tp3_value / sl_value if sl_value else 0.0
    if old_tp3_rr <= 0:
        return tp1_value, tp2_value, tp3_value
    scale = target_tp3_rr / old_tp3_rr
    return (
        round(tp1_value * scale, 6),
        round(tp2_value * scale, 6),
        round(tp3_value * scale, 6),
    )


def build_symbol_scan_configs() -> dict[str, SymbolScanConfig]:
    scaled_tp_levels = {
        "XAUUSD": scale_tp_levels_from_tp3(0.50, 0.714, 1.375, 2.2, 2.2),  # (0.357, 0.6875, 1.1)
        # Forex risk settings normalized to 2.0R while leaving SL unchanged
        "USOIL": scale_tp_levels_from_tp3(0.30, 0.1925, 0.385, 0.66, 2.2),  # (0.1925, 0.385, 0.66)
        "GBPUSD": scale_tp_levels_from_tp3(30.0, 35.0, 60.0, 80.0, 1.95),  # (25.59375, 43.875, 58.5)
        "EURUSD": scale_tp_levels_from_tp3(20.0, 15.7143, 28.2857, 44.0, 2.0),  # (14.285727, 25.714273, 40.0)
        "USDJPY": scale_tp_levels_from_tp3(30.0, 24.5, 42.0, 63.0, 2.0),  # (23.333333, 40.0, 60.0)
        "AUDUSD": scale_tp_levels_from_tp3(12.5, 10.5, 17.5, 26.25, 1.5),  # (7.5, 12.5, 18.75)
        "US30": scale_tp_levels_from_tp3(120.0, 82.5, 165.0, 264.0, 2.2),  # (82.5, 165.0, 264.0)
        "NASDAQ": (38.5, 77.0, 154.0),  # direct NASDAQ M15/ defaults for lower-risk profiling
        "US500": scale_tp_levels_from_tp3(90.0, 55.0, 110.0, 165.0, 1.8333333),  # (55.0, 110.0, 165.0)
        # BTCUSD uses a direct 2.0R profile: SL 1.0%, TP1 0.649091%, TP2 1.25%, TP3 2.0%
        "BTCUSD": (0.649091, 1.25, 2.0),
        # ETHUSD uses a direct 2.0R profile: SL 1.5%, TP1 1.0%, TP2 2.0%, TP3 3.0%
        "ETHUSD": (1.0, 2.0, 3.0),
    }
    return {
        "XAUUSD": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=0.50,
            main_sl_unit="percent",
            main_tp1_value=scaled_tp_levels["XAUUSD"][0],
            main_tp1_unit="percent",
            main_tp2_value=scaled_tp_levels["XAUUSD"][1],
            main_tp2_unit="percent",
            main_tp3_value=scaled_tp_levels["XAUUSD"][2],
            main_tp3_unit="percent",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=0.1640625,
                    main_sl_unit="percent",
                    main_tp1_value=0.1839469,
                    main_tp1_unit="percent",
                    main_tp2_value=0.3789844,
                    main_tp2_unit="percent",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=0.35,
                    main_sl_unit="percent",
                    main_tp1_value=0.2499,
                    main_tp1_unit="percent",
                    main_tp2_value=0.48125,
                    main_tp2_unit="percent",
                    main_tp3_value=0.77,
                    main_tp3_unit="percent",
                )
            },
            entry_buffer_min=11.25,
            entry_buffer_min_unit="pips",
            entry_buffer_max=18.75,
            entry_buffer_max_unit="pips",
        ),
        "USOIL": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=0.30,
            main_sl_unit="price",
            main_tp1_value=scaled_tp_levels["USOIL"][0],
            main_tp1_unit="price",
            main_tp2_value=scaled_tp_levels["USOIL"][1],
            main_tp2_unit="price",
            main_tp3_value=scaled_tp_levels["USOIL"][2],
            main_tp3_unit="price",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=0.0984375,
                    main_sl_unit="price",
                    main_tp1_value=0.1010626,
                    main_tp1_unit="price",
                    main_tp2_value=0.2237813,
                    main_tp2_unit="price",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=0.225,
                    main_sl_unit="price",
                    main_tp1_value=0.144375,
                    main_tp1_unit="price",
                    main_tp2_value=0.28875,
                    main_tp2_unit="price",
                    main_tp3_value=0.495,
                    main_tp3_unit="price",
                )
            },
            entry_buffer_min=0.0375,
            entry_buffer_min_unit="price",
            entry_buffer_max=0.06,
            entry_buffer_max_unit="price",
        ),
        "GBPUSD": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=30.0,
            main_sl_unit="pips",
            main_tp1_value=scaled_tp_levels["GBPUSD"][0],
            main_tp1_unit="pips",
            main_tp2_value=scaled_tp_levels["GBPUSD"][1],
            main_tp2_unit="pips",
            main_tp3_value=scaled_tp_levels["GBPUSD"][2],
            main_tp3_unit="pips",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=9.84375,
                    main_sl_unit="pips",
                    main_tp1_value=12.4769,
                    main_tp1_unit="pips",
                    main_tp2_value=21.1149,
                    main_tp2_unit="pips",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=22.5,
                    main_sl_unit="pips",
                    main_tp1_value=19.1953125,
                    main_tp1_unit="pips",
                    main_tp2_value=32.90625,
                    main_tp2_unit="pips",
                    main_tp3_value=43.875,
                    main_tp3_unit="pips",
                )
            },
            entry_buffer_min=2.25,
            entry_buffer_min_unit="pips",
            entry_buffer_max=3.75,
            entry_buffer_max_unit="pips",
        ),
        "EURUSD": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=20.0,
            main_sl_unit="pips",
            main_tp1_value=scaled_tp_levels["EURUSD"][0],
            main_tp1_unit="pips",
            main_tp2_value=scaled_tp_levels["EURUSD"][1],
            main_tp2_unit="pips",
            main_tp3_value=scaled_tp_levels["EURUSD"][2],
            main_tp3_unit="pips",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=6.5625,
                    main_sl_unit="pips",
                    main_tp1_value=7.125,
                    main_tp1_unit="pips",
                    main_tp2_value=13.875,
                    main_tp2_unit="pips",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=15.0,
                    main_sl_unit="pips",
                    main_tp1_value=10.7142952,
                    main_tp1_unit="pips",
                    main_tp2_value=19.2857047,
                    main_tp2_unit="pips",
                    main_tp3_value=30.0,
                    main_tp3_unit="pips",
                )
            },
            entry_buffer_min=1.5,
            entry_buffer_min_unit="pips",
            entry_buffer_max=3.0,
            entry_buffer_max_unit="pips",
        ),
        "USDJPY": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=30.0,
            main_sl_unit="pips",
            main_tp1_value=scaled_tp_levels["USDJPY"][0],
            main_tp1_unit="pips",
            main_tp2_value=scaled_tp_levels["USDJPY"][1],
            main_tp2_unit="pips",
            main_tp3_value=scaled_tp_levels["USDJPY"][2],
            main_tp3_unit="pips",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=9.84375,
                    main_sl_unit="pips",
                    main_tp1_value=11.375,
                    main_tp1_unit="pips",
                    main_tp2_value=21.0,
                    main_tp2_unit="pips",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=22.5,
                    main_sl_unit="pips",
                    main_tp1_value=17.4999997,
                    main_tp1_unit="pips",
                    main_tp2_value=30.0,
                    main_tp2_unit="pips",
                    main_tp3_value=45.0,
                    main_tp3_unit="pips",
                )
            },
            entry_buffer_min=2.25,
            entry_buffer_min_unit="pips",
            entry_buffer_max=3.75,
            entry_buffer_max_unit="pips",
        ),
        "AUDUSD": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=12.5,
            main_sl_unit="pips",
            main_tp1_value=scaled_tp_levels["AUDUSD"][0],
            main_tp1_unit="pips",
            main_tp2_value=scaled_tp_levels["AUDUSD"][1],
            main_tp2_unit="pips",
            main_tp3_value=scaled_tp_levels["AUDUSD"][2],
            main_tp3_unit="pips",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=4.1015625,
                    main_sl_unit="pips",
                    main_tp1_value=3.609375,
                    main_tp1_unit="pips",
                    main_tp2_value=6.5625,
                    main_tp2_unit="pips",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=9.375,
                    main_sl_unit="pips",
                    main_tp1_value=5.625,
                    main_tp1_unit="pips",
                    main_tp2_value=9.375,
                    main_tp2_unit="pips",
                    main_tp3_value=14.0625,
                    main_tp3_unit="pips",
                )
            },
            entry_buffer_min=1.5,
            entry_buffer_min_unit="pips",
            entry_buffer_max=3.0,
            entry_buffer_max_unit="pips",
        ),
        "US30": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=False,
            scan_interval_seconds=60,
            main_sl_value=120.0,
            main_sl_unit="points",
            main_tp1_value=scaled_tp_levels["US30"][0],
            main_tp1_unit="points",
            main_tp2_value=scaled_tp_levels["US30"][1],
            main_tp2_unit="points",
            main_tp3_value=scaled_tp_levels["US30"][2],
            main_tp3_unit="points",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=39.375,
                    main_sl_unit="points",
                    main_tp1_value=43.3125,
                    main_tp1_unit="points",
                    main_tp2_value=90.95625,
                    main_tp2_unit="points",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=90.0,
                    main_sl_unit="points",
                    main_tp1_value=61.875,
                    main_tp1_unit="points",
                    main_tp2_value=123.75,
                    main_tp2_unit="points",
                    main_tp3_value=198.0,
                    main_tp3_unit="points",
                )
            },
            entry_buffer_min=11.25,
            entry_buffer_min_unit="points",
            entry_buffer_max=18.75,
            entry_buffer_max_unit="points",
        ),
        "NASDAQ": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,   # only trade with H1 trend — filters counter-trend losses
            scan_interval_seconds=60,
            main_sl_value=70.0,
            main_sl_unit="points",
            main_tp1_value=scaled_tp_levels["NASDAQ"][0],
            main_tp1_unit="points",
            main_tp2_value=scaled_tp_levels["NASDAQ"][1],
            main_tp2_unit="points",
            main_tp3_value=scaled_tp_levels["NASDAQ"][2],
            main_tp3_unit="points",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=22.96875,
                    main_sl_unit="points",
                    main_tp1_value=20.2125,
                    main_tp1_unit="points",
                    main_tp2_value=50.53125,
                    main_tp2_unit="points",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=45.0,
                    main_sl_unit="points",
                    main_tp1_value=28.875,
                    main_tp1_unit="points",
                    main_tp2_value=57.75,
                    main_tp2_unit="points",
                    main_tp3_value=115.5,
                    main_tp3_unit="points",
                )
            },
            entry_buffer_min=3.75,
            entry_buffer_min_unit="points",
            entry_buffer_max=7.5,
            entry_buffer_max_unit="points",
        ),
        "US500": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,   # trade with H1 trend for better direction alignment
            scan_interval_seconds=60,
            main_sl_value=90.0,
            main_sl_unit="points",
            main_tp1_value=scaled_tp_levels["US500"][0],
            main_tp1_unit="points",
            main_tp2_value=scaled_tp_levels["US500"][1],
            main_tp2_unit="points",
            main_tp3_value=scaled_tp_levels["US500"][2],
            main_tp3_unit="points",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=22.1484375,
                    main_sl_unit="points",
                    main_tp1_value=23.625,
                    main_tp1_unit="points",
                    main_tp2_value=51.1875,
                    main_tp2_unit="points",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=50.625,
                    main_sl_unit="points",
                    main_tp1_value=31.5,
                    main_tp1_unit="points",
                    main_tp2_value=63.0,
                    main_tp2_unit="points",
                    main_tp3_value=94.5,
                    main_tp3_unit="points",
                )
            },
            entry_buffer_min=7.5,
            entry_buffer_min_unit="points",
            entry_buffer_max=15.0,
            entry_buffer_max_unit="points",
        ),
        "BTCUSD": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=1.0,
            main_sl_unit="percent",
            main_tp1_value=0.649091,
            main_tp1_unit="percent",
            main_tp2_value=1.25,
            main_tp2_unit="percent",
            main_tp3_value=2.0,
            main_tp3_unit="percent",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=0.328125,
                    main_sl_unit="percent",
                    main_tp1_value=0.3344625,
                    main_tp1_unit="percent",
                    main_tp2_value=0.6890625,
                    main_tp2_unit="percent",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=0.60,
                    main_sl_unit="percent",
                    main_tp1_value=0.39,
                    main_tp1_unit="percent",
                    main_tp2_value=0.75,
                    main_tp2_unit="percent",
                    main_tp3_value=1.20,
                    main_tp3_unit="percent",
                )
            },
            entry_buffer_min=0.075,
            entry_buffer_min_unit="percent",
            entry_buffer_max=0.1125,
            entry_buffer_max_unit="percent",
        ),
        "ETHUSD": SymbolScanConfig(
            timeframes=build_timeframes(["M5", "M15"]),
            use_trend_filter=True,
            scan_interval_seconds=60,
            main_sl_value=1.5,
            main_sl_unit="percent",
            main_tp1_value=1.0,
            main_tp1_unit="percent",
            main_tp2_value=2.0,
            main_tp2_unit="percent",
            main_tp3_value=3.0,
            main_tp3_unit="percent",
            timeframe_risk_configs={
                "M1": TimeframeRiskConfig(
                    main_sl_value=0.4921875,
                    main_sl_unit="percent",
                    main_tp1_value=0.525,
                    main_tp1_unit="percent",
                    main_tp2_value=1.05,
                    main_tp2_unit="percent",
                ),
                "M5": TimeframeRiskConfig(
                    main_sl_value=0.90,
                    main_sl_unit="percent",
                    main_tp1_value=0.60,
                    main_tp1_unit="percent",
                    main_tp2_value=1.20,
                    main_tp2_unit="percent",
                    main_tp3_value=1.80,
                    main_tp3_unit="percent",
                )
            },
            entry_buffer_min=0.1125,
            entry_buffer_min_unit="percent",
            entry_buffer_max=0.15,
            entry_buffer_max_unit="percent",
        ),
    }


@dataclass(slots=True)
class ScannerConfig:
    bars_to_fetch: int = 1500
    chart_bars: int = 140
    scan_interval_seconds: int = 30
    timeframes: list[TimeframeConfig] = field(
        default_factory=lambda: build_timeframes(DEFAULT_TIMEFRAME_NAMES)
    )
    watchlist: list[str] = field(default_factory=lambda: DEFAULT_WATCHLIST.copy())
    symbol_scan_configs: dict[str, SymbolScanConfig] = field(default_factory=build_symbol_scan_configs)
    output_dir: Path = Path("signals")
    enable_sound: bool = True
    print_alerts: bool = True
    enable_swift: bool = True
    main_enabled: bool = True
    main_use_alternate_signals: bool = True
    main_alternate_multiplier: int = 4
    main_basis_type: str = "ALMA"
    main_basis_length: int = 1
    main_offset_sigma: int = 3
    main_offset_alma: float = 0.75
    main_delay_offset: int = 0
    main_trade_type: str = "BOTH"
    main_heikin_ashi: bool = False
    main_tp1_percent: float = 1.0
    main_tp2_percent: float = 1.5
    main_tp3_percent: float = 2.0
    main_sl_percent: float = 0.5
    main_tp1_delta: float | None = None
    main_tp2_delta: float | None = None
    main_tp3_delta: float | None = None
    main_sl_delta: float | None = None
    enable_dynamic_levels: bool = False
    dynamic_level_lookback: int = 20
    atr_period: int = 14
    dynamic_sl_atr_multiplier: float = 1.5
    dynamic_tp_atr_multiplier: float = 3.0
    enable_execution: bool = True
    entry_pip_buffer: float = 2.0
    tp_target_level: str = "tp3"
    order_volume: float = 0.02
    order_magic_number: int = 123456
    order_deviation: int = 20
    max_confirmation_attempts: int = 1
    # Risk reduction: when True, actual order volume is multiplied by `reduce_position_fraction`.
    reduce_position_size: bool = True
    reduce_position_fraction: float = 0.5
    # Treat symbols as tradeable even if MT5 reports trade_allowed=False
    force_trade_allowed: bool = True
    use_trend_filter: bool = False
    # Displacement gating has been removed; all valid SWIFT cross entries are accepted.
    trend_period: int = 14
    trend_multiplier: float = 1.5
    # --- Risk management additions ---
    # 1. Daily loss circuit breaker
    daily_max_loss_percent: float = 3.0          # pause trading after losing this % of starting equity in a day
    daily_loss_breaker_enabled: bool = True
    # 2. Correlated symbol exposure cap
    max_correlated_open_positions: int = 1        # max simultaneous open positions within a correlation group
    correlation_groups: list[list[str]] = field(default_factory=lambda: [
        ["NASDAQ", "US30"],
        ["XAUUSD", "AUDUSD"],
        ["BTCUSD", "ETHUSD"],
    ])
    # 3. News blackout window (blocks NEW entries near a scheduled high-impact event)
    news_blackout_enabled: bool = True
    news_blackout_minutes_before: int = 15
    news_blackout_minutes_after: int = 15
    # 3b. News flatten (protects positions that were ALREADY OPEN before a scheduled
    # high-impact event -- the blackout above only stops new entries, it does not
    # close or shield a trade that was opened earlier in the day and is still open
    # when the news hits)
    news_flatten_enabled: bool = True
    news_flatten_minutes_before: int = 30
    # 4. Equity-based position sizing
    use_equity_based_sizing: bool = False
    risk_percent_per_trade: float = 1.0            # % of equity risked per trade when equity sizing is on
    # 5. Weekend gap protection for crypto
    weekend_flatten_enabled: bool = True
    weekend_flatten_symbols: list[str] = field(default_factory=lambda: ["BTCUSD", "ETHUSD"])
    weekend_flatten_day_gmt: int = 4               # 4 = Friday (Mon=0)
    weekend_flatten_hour_gmt: int = 20              # flatten crypto positions at/after this GMT hour on Friday

    # Reversal handling: small buffer (in pips/points) before triggering reversal-exit
    reversal_buffer_pips: float = 0.0
    # If True, enable automatic reversal-based exit after TP2/TP1 logic
    reversal_exit_enabled: bool = True
    # If True, log reversal watch/exit events to output_dir/reversal_events.jsonl
    reversal_alerts_enabled: bool = True
    # If True, close the remaining position when the trade is near SL and the setup is invalidated.
    stop_loss_proximity_exit_enabled: bool = True
    # When configured, only symbols in this list are subject to the near-SL exit rule.
    stop_loss_proximity_exit_symbols: list[str] = field(default_factory=lambda: DEFAULT_WATCHLIST.copy())
    # Only apply proximity exits for these source signal timeframes.
    stop_loss_proximity_exit_timeframes: list[str] = field(default_factory=lambda: ["M5", "M15"])
    # Fraction of the original SL distance that defines the near-SL danger zone.
    stop_loss_proximity_exit_ratio: float = 0.25
    # Signal quality scoring (0-100 scale, 0 = no filtering, useful for analysis)
    # Scores based on: momentum, H4 alignment, entry freshness, candle commitment
    min_signal_quality_score: int = 0               # 0 = accept all, 50+ = strict filtering
    backtest_start_year: int = 2020
    backtest_start_month: int = 1
    backtest_start_day: int = 1
    backtest_end_year: int = 2026
    backtest_end_month: int = 12
    backtest_end_day: int = 31
    # Minimum seconds between alerts for the same symbol to avoid message spam
    alert_cooldown_seconds: int = 30
    telegram_bot_token: str | None = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN"))
    telegram_chat_ids: list[str] = field(
        default_factory=lambda: parse_chat_ids(
            os.getenv("TELEGRAM_CHAT_IDS"),
            os.getenv("TELEGRAM_CHAT_ID"),
        )
    )
    telegram_silent: bool = False
    telegram_only: bool = False
    # When repeated SSL errors occur during Telegram image uploads, after
    # `telegram_ssl_error_threshold` consecutive SSLErrors the notifier will
    # switch to text-only mode for `telegram_text_only_cooldown_seconds` seconds.
    telegram_ssl_error_threshold: int = 3
    telegram_text_only_cooldown_seconds: int = 300
