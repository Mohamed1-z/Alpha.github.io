from pathlib import Path
import sys
import warnings

workspace_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(workspace_root))

from swift_scanner.config import ScannerConfig, TimeframeConfig
from swift_scanner.indicator import detect_swift_signals, _alternate_series, _crossover, _crossunder
from swift_scanner.mt5_client import MT5Client

warnings.filterwarnings("ignore")

cfg = ScannerConfig(enable_execution=False, output_dir=Path("signals"))
tf = TimeframeConfig(name="M15", mt5_code=1)

with MT5Client(cfg) as mt5:
    matches = mt5.resolve_watchlist()
    for match in matches:
        requested = match.requested
        actual = match.actual or requested
        bars = mt5.fetch_rates(actual, tf)
        print("\n===", requested, "->", actual)
        if bars is None or len(bars) < 20:
            print("not enough bars", 0 if bars is None else len(bars))
            continue
        data = bars.copy().reset_index(drop=True)
        data["time"] = data["time"].astype("datetime64[ns, UTC]")
        for column in ["open", "high", "low", "close", "volume"]:
            data[column] = data[column].astype(float)
        main_close, main_open = _alternate_series(
            data,
            "M15",
            cfg.main_use_alternate_signals,
            cfg.main_alternate_multiplier,
            cfg.main_basis_type,
            cfg.main_basis_length,
            cfg.main_offset_sigma,
            cfg.main_offset_alma,
            cfg.main_delay_offset,
            cfg.main_heikin_ashi,
        )
        print("signal source by sample")
        print(" last 5 closes:", data["close"].tail(5).tolist())
        print(" last 5 opens:", data["open"].tail(5).tolist())
        if cfg.main_use_alternate_signals:
            from swift_scanner.indicator import _resample_ohlc
            signal_frame = _resample_ohlc(data, "M15", cfg.main_alternate_multiplier)
            print(" resampled frame tail:")
            print(signal_frame.tail(10).to_string(index=False))
        print(" last 20 main_close:", main_close.tail(20).tolist())
        print(" last 20 main_open:", main_open.tail(20).tolist())
        print(" last 20 cross_over:", _crossover(main_close, main_open).tail(20).tolist())
        print(" last 20 cross_under:", _crossunder(main_close, main_open).tail(20).tolist())

        working, events, zones = detect_swift_signals(
            data, requested, actual, "M15", cfg
        )
        last = len(working) - 1
        last_condition = working["swift_main_condition"].iloc[last]
        last_entry = working["swift_main_entry"].iloc[last]
        print("last idx", last, "condition", last_condition, "entry", last_entry)
        print("last 8 swift_main_condition:", working["swift_main_condition"].tail(8).tolist())
        print("last 8 swift_main_entry:", working["swift_main_entry"].tail(8).tolist())
        print("events total", len(events))
        for e in events:
            print(
                "event", e.signal_label,
                "formed", e.zone.formed_index,
                "triggered", e.triggered_at,
                "dir", e.zone.direction,
            )
        if not events:
            print("no events")
        recent = [e for e in events if e.zone.formed_index >= max(0, last - 8)]
        print("recent count", len(recent))
        print("active zones", len(zones))
