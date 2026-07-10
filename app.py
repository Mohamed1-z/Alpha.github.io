from __future__ import annotations

import argparse
import os
from pathlib import Path

from swift_scanner.config import DEFAULT_TIMEFRAME_NAMES, ScannerConfig, TIMEFRAME_MAP, build_timeframes
from swift_scanner.config_validation import validate_config
from swift_scanner.notifier import SignalNotifier
from swift_scanner.reporting import analyze_alert_history
from swift_scanner.watcher import SwiftWatcher
from swift_scanner.charting import generate_timeframe_example_image

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(dotenv_path: str | Path = ".env") -> bool:
        """Small fallback loader so python-dotenv is optional."""
        env_path = Path(dotenv_path)
        if not env_path.exists():
            return False

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SWIFT MERGED watchlist scanner converted from the provided Pine Script strategy."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan of the watchlist and exit.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between scan cycles when running continuously.",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=1500,
        help="How many historical candles to request from MetaTrader 5 per symbol and timeframe.",
    )
    parser.add_argument(
        "--chart-bars",
        type=int,
        default=140,
        help="How many recent bars to show in each signal image.",
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=DEFAULT_TIMEFRAME_NAMES,
        choices=sorted(TIMEFRAME_MAP),
        help="Detection timeframes to scan. Defaults to M15.",
    )
    parser.add_argument(
        "--output-dir",
        default="signals",
        help="Folder used for alert images and scanner state.",
    )
    parser.add_argument(
        "--mute",
        action="store_true",
        help="Disable the local Windows beep alert.",
    )
    parser.add_argument(
        "--telegram-only",
        action="store_true",
        help="Send alerts only to Telegram and disable local sound alerts.",
    )
    parser.add_argument(
        "--telegram-silent",
        action="store_true",
        help="Send Telegram notifications silently.",
    )
    parser.add_argument(
        "--disable-swift",
        action="store_true",
        help="Disable SWIFT detection so it can be enabled later without removing code.",
    )
    parser.add_argument(
        "--trend-filter",
        action="store_true",
        help="Require the SWIFT range-filter confirmation for main entries.",
    )
    parser.add_argument(
        "--disable-alternate-signals",
        action="store_true",
        help="Use the chart timeframe directly instead of the Pine alternate timeframe multiplier.",
    )
    parser.add_argument(
        "--main-alt-multiplier",
        type=int,
        default=4,
        help="Main signal alternate timeframe multiplier. Defaults to 4.",
    )
    parser.add_argument(
        "--main-ma",
        choices=["TEMA", "HullMA", "ALMA"],
        default="ALMA",
        help="Main moving-average type. Defaults to ALMA.",
    )
    parser.add_argument(
        "--enable-dynamic-levels",
        action="store_true",
        help="Use dynamic ATR/swing-based SL and TP levels instead of fixed percentages.",
    )
    parser.add_argument(
        "--dynamic-level-lookback",
        type=int,
        default=20,
        help="Number of bars used to compute dynamic support/resistance and ATR. Defaults to 20.",
    )
    parser.add_argument(
        "--atr-period",
        type=int,
        default=14,
        help="ATR lookback period for dynamic level calculation. Defaults to 14.",
    )
    parser.add_argument(
        "--dynamic-sl-atr-multiplier",
        type=float,
        default=1.5,
        help="ATR multiplier used to position dynamic stop-loss. Defaults to 1.5.",
    )
    parser.add_argument(
        "--dynamic-tp-atr-multiplier",
        type=float,
        default=3.0,
        help="ATR multiplier used for the dynamic primary take-profit. Defaults to 3.0.",
    )
    parser.add_argument(
        "--entry-pip-buffer",
        type=float,
        default=2.0,
        help=(
            "Global fallback entry buffer in pips when a symbol does not have a configured "
            "per-symbol entry buffer. Defaults to 2.0."
        ),
    )
    parser.add_argument(
        "--disable-execution",
        action="store_true",
        help="Disable live market order placement in MetaTrader 5. Enabled by default.",
    )
    parser.add_argument(
        "--order-volume",
        type=float,
        default=0.02,
        help="Order volume/lot size for live execution. Defaults to 0.02.",
    )
    parser.add_argument(
        "--tp-target-level",
        choices=["auto", "tp1", "tp2", "tp3"],
        default="tp3",
        help="Which TP level should be used as the effective take-profit target for execution and alerts. Defaults to tp3 for full partial-close ladder behavior.",
    )
    parser.add_argument(
        "--order-magic-number",
        type=int,
        default=123456,
        help="Magic number to tag live MT5 orders. Defaults to 123456.",
    )
    parser.add_argument(
        "--max-confirmation-attempts",
        type=int,
        default=1,
        help="How many retry attempts to allow for a pending confirmation before giving up. Defaults to 1.",
    )
    parser.add_argument(
        "--order-deviation",
        type=int,
        default=20,
        help="Maximum deviation in points for MT5 order execution. Defaults to 20.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test Telegram message and exit.",
    )
    parser.add_argument(
        "--send-sample-timeframe-image",
        action="store_true",
        help="Generate and send an example timeframe image to Telegram, then exit.",
    )
    parser.add_argument(
        "--export-alert-report",
        action="store_true",
        help="Generate an Excel report from historical Telegram alert events and save it to the output directory.",
    )
    parser.add_argument(
        "--report-lookahead-bars",
        type=int,
        default=40,
        help="How many future candles each report analysis should inspect. Defaults to 40.",
    )
    parser.add_argument(
        "--send-report-to-telegram",
        action="store_true",
        help="Send the generated alert report Excel file to configured Telegram chat IDs.",
    )
    return parser


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    enable_swift = not args.disable_swift

    config = ScannerConfig(
        bars_to_fetch=args.bars,
        chart_bars=args.chart_bars,
        scan_interval_seconds=args.interval,
        timeframes=build_timeframes(args.timeframes),
        output_dir=Path(args.output_dir),
        enable_sound=not args.mute and not args.telegram_only,
        print_alerts=not args.telegram_only,
        telegram_silent=args.telegram_silent,
        telegram_only=args.telegram_only,
        enable_swift=enable_swift,
        main_use_alternate_signals=not args.disable_alternate_signals,
        main_alternate_multiplier=args.main_alt_multiplier,
        main_basis_type=args.main_ma,
        enable_dynamic_levels=args.enable_dynamic_levels,
        dynamic_level_lookback=args.dynamic_level_lookback,
        atr_period=args.atr_period,
        dynamic_sl_atr_multiplier=args.dynamic_sl_atr_multiplier,
        dynamic_tp_atr_multiplier=args.dynamic_tp_atr_multiplier,
        enable_execution=not args.disable_execution,
        entry_pip_buffer=args.entry_pip_buffer,
        tp_target_level=args.tp_target_level,
        order_volume=args.order_volume,
        order_magic_number=args.order_magic_number,
        order_deviation=args.order_deviation,
        max_confirmation_attempts=args.max_confirmation_attempts,
        use_trend_filter=args.trend_filter,
    )
    try:
        validate_config(config)
    except ValueError as exc:
        print(exc)
        raise SystemExit(1) from exc

    notifier = SignalNotifier(config)

    if args.test_telegram:
        try:
            delivered = notifier.send_test_message()
            print(f"Telegram test message sent successfully to {delivered} chat(s).")
            return
        except RuntimeError as exc:
            print(exc)
            raise SystemExit(1) from exc

    if args.send_sample_timeframe_image:
        try:
            image_path = generate_timeframe_example_image(config.output_dir)
            caption = (
                "SWIFT example timeframe chart:\n"
                "Top: M5 example series, bottom: M15 example series. "
                "Use this to compare relative timeframe structure visually."
            )
            delivered = notifier.send_photo(image_path, caption=caption)
            print(f"Sample timeframe image sent successfully to {delivered} chat(s).")
            return
        except RuntimeError as exc:
            print(exc)
            raise SystemExit(1) from exc

    if args.export_alert_report:
        report_path = config.output_dir / "alert_report.xlsx"
        history_path = config.output_dir / "alert_history.jsonl"
        try:
            output_path = analyze_alert_history(
                config,
                history_path,
                report_path,
                lookahead_bars=args.report_lookahead_bars,
            )
            print(f"Alert report generated: {output_path}")
            if args.send_report_to_telegram:
                delivered = notifier.send_document(
                    output_path,
                    caption="Alert report: direction and TP/SL outcome analysis for generated signals.",
                )
                print(f"Alert report sent to Telegram: {delivered} chat(s)")
            return
        except Exception as exc:
            print(exc)
            raise SystemExit(1) from exc

    watcher = SwiftWatcher(config)
    execution_state = "ENABLED" if config.enable_execution else "disabled"
    print(
        f"Live MT5 execution: {execution_state} "
        f"(volume={config.order_volume}, global buffer={config.entry_pip_buffer} pips)"
    )
    print("Note: per-symbol entry-buffer overrides may replace the global buffer for specific pairs.")
    try:
        watcher.run(once=args.once)
    except RuntimeError as exc:
        print(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
