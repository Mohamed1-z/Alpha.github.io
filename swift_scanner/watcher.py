from __future__ import annotations

import json
import time
from dataclasses import fields, replace, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

import pandas as pd

from swift_scanner.charting import save_signal_chart
from swift_scanner.config import ScannerConfig, SymbolScanConfig, TimeframeConfig, resolve_symbol_alias, TIMEFRAME_MAP
from swift_scanner.indicator import scan_swift
from swift_scanner.models import SignalEvent, ScanResult
from swift_scanner.mt5_client import AutoTradingDisabledError, MT5Client
from swift_scanner.news_calendar import refresh_watcher_news_events
from swift_scanner.notifier import SignalNotifier


ExecutionStatus = Literal["placed", "pending", "skipped"]


@dataclass(slots=True)
class PositionTracker:
    """Track an open position and its TP closure status."""
    ticket: int
    symbol: str
    direction: str  # 'bullish' or 'bearish'
    entry_price: float
    volume: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float | None
    tp1_qty: float  # volume to close at TP1
    tp2_qty: float  # volume to close at TP2
    tp3_qty: float = 0.0  # volume to close at TP3
    timeframe: str = ""
    tp1_closed: bool = False
    tp2_closed: bool = False
    tp3_closed: bool = False
    highest_bid: float | None = None
    lowest_ask: float | None = None
    tp1_reversal_armed: bool = False
    tp2_reversal_armed: bool = False
    breakeven_set: bool = False
    opposite_pending_closed: set[str] = field(default_factory=set)

    def get_remaining_volume(self) -> float:
        """Get the remaining unclosed volume."""
        closed = 0.0
        if self.tp1_closed:
            closed += self.tp1_qty
        if self.tp2_closed:
            closed += self.tp2_qty
        if self.tp3_closed:
            closed += self.tp3_qty
        return max(0.0, self.volume - closed)


class RuntimeConfig(NamedTuple):
    cfg: ScannerConfig
    raw_overrides: dict[str, tuple[float, str]]
    entry_buffer_override: dict[str, tuple[float, str]] | None = None

    def __getattr__(self, item: str) -> Any:
        return getattr(self.cfg, item)

    def __repr__(self) -> str:
        return f"RuntimeConfig(cfg={self.cfg!r}, raw_overrides={self.raw_overrides!r})"


# ---------------------------------------------------------------------------
# Session / killzone filter (GMT hours)
# Signals outside these windows are lower probability for SMC setups.
# ---------------------------------------------------------------------------
KILLZONE_HOURS_GMT: dict[str, list[tuple[int, int]]] = {
    # London + NY overlap only for forex majors
    "GBPUSD": [(7, 12), (12, 17)],
    "EURUSD": [(7, 12), (12, 17)],
    "USDJPY": [(7, 12), (12, 17)],
    "AUDUSD": [(0, 4), (7, 12), (12, 17)],   # Asian session valid too
    # Indices — market hours only
    "NASDAQ": [(13, 16)],   # NY open only — London open fakeouts are common on indices
    "US30":   [(13, 21)],
    # Commodities — broader window
    "XAUUSD": [(7, 12), (12, 17)],
    "USOIL":  [(13, 21)],
    # Crypto — 24/7, no restriction
    "BTCUSD": [(0, 24)],
    "ETHUSD": [(0, 24)],
}


def is_in_killzone(symbol: str) -> bool:
    """Return True if current GMT hour falls within the symbol's valid trading window."""
    canonical = symbol.upper().rstrip("mM").rstrip(".")
    # strip broker suffixes like 'BTCUSDm' -> 'BTCUSD'
    for key in KILLZONE_HOURS_GMT:
        if canonical.startswith(key):
            canonical = key
            break
    windows = KILLZONE_HOURS_GMT.get(canonical)
    if not windows:
        return True  # unknown symbol — don't block
    now_gmt_hour = datetime.now(timezone.utc).hour
    return any(start <= now_gmt_hour < end for start, end in windows)


class SwiftWatcher:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self.notifier = SignalNotifier(config)
        self.state_file = self.config.output_dir / "alert_state.json"
        self.position_state_file = self.config.output_dir / "open_positions.json"
        self.history_file = self.config.output_dir / "alert_history.jsonl"
        self.sent_alert_ids = self.load_state()
        # Track last alert timestamp per symbol to throttle repeated alerts
        self.last_alert_time: dict[str, float] = {}
        # Track the latest known SWIFT signal IDs per symbol/timeframe.
        self.last_swift_event_ids: dict[tuple[str, str], set[str]] = {}
        # Track the last known SWIFT direction per symbol/timeframe.
        self.last_signal_direction: dict[tuple[str, str], str] = {}
        # Track the last scan time per symbol to support custom symbol intervals.
        self.last_symbol_scan_time: dict[str, float] = {}
        # Track open positions for partial closes at TP levels
        self.open_positions: dict[int, PositionTracker] = self.load_position_trackers()
        # Entry signals waiting for the configured pip buffer before execution.
        self.pending_execution_events: dict[str, tuple[SignalEvent, dict[str, tuple[float, str]] | None, ScanResult]] = {}
        self.pending_confirmation_attempts: dict[str, int] = {}
        # --- Risk management state ---
        self.daily_start_equity: float | None = None
        self.daily_start_date: str | None = None
        self.trading_paused_for_day: bool = False
        self.weekend_flatten_done_this_week: bool = False
        self.last_news_refresh_time: float = 0.0
        # Tracks (event_time_iso, symbol) pairs already flattened so we don't
        # repeatedly try to close the same position for the same event.
        self.news_flattened_events: set[tuple[str, str]] = set()

    def load_state(self) -> set[str]:
        if not self.state_file.exists():
            return set()
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return set()
        return set(payload.get("sent_alert_ids", []))

    def save_state(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        payload = {"sent_alert_ids": sorted(self.sent_alert_ids)[-5000:]}
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_position_trackers(self) -> dict[int, PositionTracker]:
        if not self.position_state_file.exists():
            return {}
        try:
            payload = json.loads(self.position_state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

        trackers: dict[int, PositionTracker] = {}
        valid_fields = {field.name for field in fields(PositionTracker)}
        for raw_tracker in payload.get("open_positions", []):
            if not isinstance(raw_tracker, dict):
                continue
            try:
                values = {}
                for key in valid_fields:
                    if key not in raw_tracker:
                        continue
                    value = raw_tracker[key]
                    if key == "opposite_pending_closed" and isinstance(value, list):
                        value = set(value)
                    values[key] = value
                tracker = PositionTracker(**values)
                trackers[int(tracker.ticket)] = tracker
            except (TypeError, ValueError):
                continue
        return trackers

    def save_position_trackers(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        tracker_fields = [field.name for field in fields(PositionTracker)]
        payload = {
            "open_positions": [
                {
                    field_name: list(getattr(tracker, field_name))
                    if field_name == "opposite_pending_closed"
                    else getattr(tracker, field_name)
                    for field_name in tracker_fields
                    if hasattr(tracker, field_name)
                }
                for tracker in self.open_positions.values()
            ]
        }
        self.position_state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def run(self, once: bool = False) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.enable_swift:
            print("SWIFT scanning is disabled. Enable SWIFT detection before running.")
            return

        if self.config.telegram_only:
            self.notifier.validate_telegram()

        with MT5Client(self.config) as mt5_client:
            symbol_matches = mt5_client.resolve_watchlist()
            self.print_watchlist(symbol_matches)
            print(
                "Detection timeframes: "
                + ", ".join(timeframe.name for timeframe in self.config.timeframes)
            )

            while True:
                cycle_started = time.time()
                if cycle_started - self.last_news_refresh_time >= 3600:
                    refresh_watcher_news_events(self)
                    self.last_news_refresh_time = cycle_started

                # --- Weekend gap protection: flatten crypto before weekend close ---
                self.check_weekend_flatten(mt5_client)
                # --- News protection: flatten positions ahead of scheduled high-impact events ---
                self.check_news_flatten(mt5_client)
                for match in symbol_matches:
                    if not match.actual:
                        print(f"Skipping {match.requested}: no matching MT5 symbol found.")
                        continue

                    symbol_config = self.config.symbol_scan_configs.get(
                        resolve_symbol_alias(match.actual or match.requested)
                        or (match.actual or match.requested)
                    )

                    if symbol_config and symbol_config.scan_interval_seconds:
                        last_scan = self.last_symbol_scan_time.get(match.actual, 0)
                        time_since_last_scan = time.time() - last_scan
                        if time_since_last_scan < symbol_config.scan_interval_seconds:
                            if self.config.print_alerts:
                                print(
                                    f"Skipping {match.requested} ({match.actual}) scan: next scan in {int(symbol_config.scan_interval_seconds - time_since_last_scan)}s"
                                )
                            continue

                    timeframes = symbol_config.timeframes if symbol_config else self.config.timeframes
                    for timeframe in timeframes:
                        effective_runtime, effective_raw, entry_buffer = self._build_runtime_config(
                            symbol_config,
                            timeframe_name=timeframe.name,
                        )
                        try:
                            self.process_symbol(
                                mt5_client,
                                match.requested,
                                match.actual,
                                timeframe,
                                effective_runtime,
                                effective_raw,
                                entry_buffer,
                            )
                        except Exception as exc:
                            print(
                                f"{match.requested} ({match.actual}) {timeframe.name} scan failed: {exc}"
                            )
                    self.last_symbol_scan_time[match.actual] = time.time()

                self.retry_pending_executions(mt5_client)
                self.save_state()
                if once:
                    return

                # Monitor open positions for TP-based partial closes
                try:
                    self._monitor_positions(mt5_client)
                except Exception as exc:
                    print(f"Position monitoring failed: {exc}")

                elapsed = time.time() - cycle_started
                sleep_seconds = max(5, self.config.scan_interval_seconds - int(elapsed))
                print(f"Cycle complete. Sleeping {sleep_seconds}s before the next scan.")
                time.sleep(sleep_seconds)

    def process_symbol(
        self,
        mt5_client: MT5Client,
        requested: str,
        actual: str,
        timeframe: TimeframeConfig,
        config: ScannerConfig | RuntimeConfig,
        raw_overrides: dict[str, tuple[float, str]] | None = None,
        entry_buffer_override: dict[str, tuple[float, str]] | None = None,
    ) -> None:
        bars = mt5_client.fetch_rates(actual, timeframe)

        # If there are absolute raw overrides (price/points/pips), convert them
        # to percent-based overrides using the latest close so the scanner
        # generates events that match execution overrides.
        cfg_for_scan = config.cfg if isinstance(config, RuntimeConfig) else config
        if raw_overrides:
            overrides: dict[str, float] = {}
            try:
                latest_close = float(bars.iloc[-1]["close"])
            except Exception:
                latest_close = None
            try:
                symbol_info: Any = mt5_client.get_symbol_market_status(actual)
                point_val = symbol_info.get("point", 0.0001) if isinstance(symbol_info, dict) else getattr(symbol_info, "point", 0.0001)
                point = self._as_optional_float(point_val) or 0.0001
                digits = self._as_optional_int(symbol_info.get("digits") if isinstance(symbol_info, dict) else getattr(symbol_info, "digits", None))
            except Exception:
                point = 0.0001
                digits = None

            if latest_close is not None:
                percent_fields_by_prefix = {
                    "main_tp1": ("main_tp1_percent",),
                    "main_tp2": ("main_tp2_percent",),
                    "main_tp3": ("main_tp3_percent",),
                    "main_sl": ("main_sl_percent",),
                }
                delta_fields_by_prefix = {
                    "main_tp1": ("main_tp1_delta",),
                    "main_tp2": ("main_tp2_delta",),
                    "main_tp3": ("main_tp3_delta",),
                    "main_sl": ("main_sl_delta",),
                }
                for prefix, (val, unit) in raw_overrides.items():
                    unit = unit.lower()
                    try:
                        if unit == "percent":
                            pct = float(val)
                            for field_name in percent_fields_by_prefix.get(prefix, ()):
                                overrides[field_name] = pct
                        elif unit in ("price", "pips", "pip", "points", "pts"):
                            delta = self._convert_price_delta(
                                latest_close,
                                float(val),
                                unit,
                                point,
                                digits,
                            )
                            for field_name in delta_fields_by_prefix.get(prefix, ()):
                                overrides[field_name] = delta
                        else:
                            continue
                    except Exception:
                        continue

            if overrides:
                base_cfg: ScannerConfig = config.cfg if isinstance(config, RuntimeConfig) else config
                cfg_for_scan = replace(base_cfg, **overrides)

        result = scan_swift(bars, requested, actual, timeframe.name, cfg_for_scan)
        new_swift_events = self._filter_redundant_swift_events(
            requested, actual, timeframe.name, result.fresh_events
        )
        print(
            f"{requested:>7} -> {actual:<15} | tf={timeframe.name:<3} | close={float(bars.iloc[-1]['close']):>12.5f} | "
            f"swift={len(new_swift_events):>2} | "
            f"last_bar={bars.iloc[-1]['time'].to_pydatetime().isoformat()}"
        )
        for event in new_swift_events:
            self.handle_event(result, event, mt5_client, raw_overrides, entry_buffer_override)

    def _build_runtime_config(
        self,
        symbol_scan_config: SymbolScanConfig | None,
        trend_filter_override: bool | None = None,
        timeframe_name: str | None = None,
    ) -> RuntimeConfig:
        overrides: dict[str, Any] = {}
        raw_overrides: dict[str, tuple[float, str]] = {}
        entry_buffer_override: dict[str, tuple[float, str]] | None = None
        if symbol_scan_config is not None:
            if symbol_scan_config.use_trend_filter is not None:
                overrides["use_trend_filter"] = symbol_scan_config.use_trend_filter
        timeframe_config = None
        if symbol_scan_config is not None and timeframe_name is not None:
            timeframe_config = symbol_scan_config.timeframe_risk_configs.get(
                str(timeframe_name).strip().upper()
            )
        config_source = timeframe_config if timeframe_config is not None else symbol_scan_config
        if config_source is not None:
            for prefix in (
                "main_tp1",
                "main_tp2",
                "main_tp3",
                "main_sl",
            ):
                val = getattr(config_source, f"{prefix}_value", None)
                unit = getattr(config_source, f"{prefix}_unit", None)
                if val is not None and unit is not None:
                    raw_overrides[prefix] = (float(val), unit.lower())
            # Collect entry buffer overrides
            entry_min = getattr(config_source, "entry_buffer_min", None)
            entry_min_unit = getattr(config_source, "entry_buffer_min_unit", None)
            entry_max = getattr(config_source, "entry_buffer_max", None)
            entry_max_unit = getattr(config_source, "entry_buffer_max_unit", None)
            if entry_min is not None and entry_min_unit is not None and entry_max is not None and entry_max_unit is not None:
                entry_buffer_override = {
                    "min": (float(entry_min), entry_min_unit.lower()),
                    "max": (float(entry_max), entry_max_unit.lower()),
                }
            if timeframe_name and raw_overrides:
                print(
                    f"Applying timeframe-specific overrides for {timeframe_name} on symbol config: "
                    f"{','.join(sorted(raw_overrides))}"
                )
        # apply absolute value overrides (value/unit) by converting to percent when possible
        # percent overrides already in 'overrides'. Return both config and raw_overrides
        if trend_filter_override is not None:
            overrides["use_trend_filter"] = trend_filter_override
        cfg = replace(self.config, **overrides) if overrides else self.config
        return RuntimeConfig(cfg, raw_overrides, entry_buffer_override)

    def _canonical_symbol_key(self, symbol: str | None) -> str | None:
        if not symbol:
            return None
        canonical = resolve_symbol_alias(symbol)
        if canonical:
            return canonical
        return str(symbol).upper().rstrip("mM").rstrip(".")

    def _event_state_key(self, event: SignalEvent) -> tuple[str | None, str]:
        symbol_key = self._canonical_symbol_key(event.symbol_actual or event.symbol_request)
        timeframe_key = self._normalize_timeframe(getattr(event, "timeframe", ""))
        return symbol_key, timeframe_key

    def handle_event(self, result, event: SignalEvent, mt5_client: MT5Client, raw_overrides: dict[str, tuple[float, str]] | None = None, entry_buffer_override: dict[str, tuple[float, str]] | None = None) -> None:
        if raw_overrides is None and self.config.enable_execution:
            symbol_key = (
                resolve_symbol_alias(event.symbol_actual or "")
                or resolve_symbol_alias(event.symbol_request or "")
                or event.symbol_request
            )
            symbol_config = self.config.symbol_scan_configs.get(symbol_key)
            if symbol_config is not None:
                _, raw_overrides, _ = self._build_runtime_config(
                    symbol_config, timeframe_name=event.timeframe
                )

        try:
            if raw_overrides:
                self._apply_raw_overrides_to_event(event, mt5_client, raw_overrides)
        except Exception as exc:
            print(
                f"Failed to apply configured risk settings for {event.symbol_request} {event.timeframe}: {exc}"
            )
            return

        # Normalize incoming event timeframe to canonical names (M15, M30, H1, H4, D1)
        try:
            normalized_tf = self._normalize_timeframe(event.timeframe)
            if normalized_tf != event.timeframe:
                print(f"[Diag] Normalizing event timeframe: raw={event.timeframe} -> normalized={normalized_tf}")
                event.timeframe = normalized_tf
        except Exception:
            pass

        if event.event_id in self.sent_alert_ids:
            return

        # Enforce per-symbol cooldown to avoid spamming Telegram
        cooldown = getattr(self.config, "alert_cooldown_seconds", 0)
        if cooldown and event.symbol_actual:
            now = time.time()
            last = self.last_alert_time.get(event.symbol_actual, 0)
            if now - last < cooldown:
                if self.config.print_alerts:
                    print(
                        f"Skipping alert for {event.symbol_actual} ({event.timeframe}), cooldown {cooldown}s"
                    )
                return

        state_key = self._event_state_key(event)
        if state_key[0]:
            previous_direction = self.last_signal_direction.get(state_key)
            if previous_direction and previous_direction != event.zone.direction:
                event.market_change_alert = True

        # --- SESSION / KILLZONE FILTER ---
        symbol_for_filter = event.symbol_actual or event.symbol_request
        if not is_in_killzone(symbol_for_filter):
            if self.config.print_alerts:
                from datetime import datetime, timezone
                gmt_hour = datetime.now(timezone.utc).hour
                print(
                    f"Skipping {symbol_for_filter} signal — outside killzone (GMT hour: {gmt_hour:02d}:xx)"
                )
            return

        try:
            if (
                self.config.enable_execution
                and event.signal_source.lower() == "swift"
                and "entry" in event.signal_label.lower()
            ):
                # Send early M1 signal alert (before execution attempt)
                if event.timeframe == "M1" and event.is_early_signal:
                    try:
                        self.notifier.send_early_m1_alert(event)
                    except Exception as exc:
                        print(f"Failed to send early M1 alert: {exc}")
                
                execution_status = self._execute_order_if_enabled(event, mt5_client, raw_overrides, entry_buffer_override)
                if execution_status == "pending":
                    image_path = save_signal_chart(result, event, self.config)
                    event.image_path = image_path
                    self.pending_execution_events[event.event_id] = (event, entry_buffer_override, result)
                    self.pending_confirmation_attempts[event.event_id] = self.pending_confirmation_attempts.get(event.event_id, 0) + 1
                    print(
                        f"Pending confirmation attempt {self.pending_confirmation_attempts[event.event_id]} for {event.symbol_actual} ({event.timeframe})"
                    )
                    return
            image_path = save_signal_chart(result, event, self.config)
            event.image_path = image_path
            self.finish_event(event, image_path)
        except Exception as exc:
            print(
                f"Alert delivery or execution failed for {event.symbol_request} {event.timeframe} "
                f"{event.signal_source}: {exc}"
            )
            return

    def finish_event(self, event: SignalEvent, image_path: Path) -> None:
        self.notifier.notify(event, image_path)
        self.record_event(event)
        self.sent_alert_ids.add(event.event_id)
        # update last alert time and direction after successful delivery
        state_key = self._event_state_key(event)
        if state_key[0]:
            self.last_alert_time[state_key[0]] = time.time()
            self.last_signal_direction[state_key] = event.zone.direction

    def record_event(self, event: SignalEvent) -> None:
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), default=str) + "\n")

    def retry_pending_executions(self, mt5_client: MT5Client) -> None:
        self._cleanup_stale_opposite_pending_ids()
        if not self.config.enable_execution or not self.pending_execution_events:
            return

        max_attempts = int(getattr(self.config, "max_confirmation_attempts", 1))
        pending_summaries: list[str] = []
        for event_id, (event, _, _) in self.pending_execution_events.items():
            attempts = self.pending_confirmation_attempts.get(event_id, 0)
            remaining = max(0, max_attempts - attempts)
            summary = (
                f"{event.symbol_actual}:{event.zone.direction}"
                f"[{event_id[:6]}] attempts={attempts}/{max_attempts}"
                f" remaining={remaining}"
            )
            pending_summaries.append(summary)
        summary_text = ", ".join(pending_summaries)
        print(
            f"Retrying {len(self.pending_execution_events)} pending executions ({summary_text})"
        )

        for event_id, (event, entry_buffer_override, result) in list(self.pending_execution_events.items()):
            if event_id in self.sent_alert_ids:
                self.pending_execution_events.pop(event_id, None)
                self.pending_confirmation_attempts.pop(event_id, None)
                continue

            try:
                status = self._execute_order_if_enabled(event, mt5_client, None, entry_buffer_override)
                if status == "pending":
                    attempts = self.pending_confirmation_attempts.get(event_id, 0) + 1
                    self.pending_confirmation_attempts[event_id] = attempts
                    max_attempts = int(getattr(self.config, "max_confirmation_attempts", 1))
                    if attempts >= max_attempts:
                        print(
                            f"Giving up on pending confirmation for {event.symbol_actual} after {attempts} attempts."
                        )
                        self.pending_execution_events.pop(event_id, None)
                        self.pending_confirmation_attempts.pop(event_id, None)
                    continue

                image_path = event.image_path
                if image_path is None:
                    image_path = save_signal_chart(result, event, self.config)
                    event.image_path = image_path
                self.finish_event(event, Path(image_path))
                self.pending_execution_events.pop(event_id, None)
                self.pending_confirmation_attempts.pop(event_id, None)
            except AutoTradingDisabledError as exc:
                print(
                    f"Pending live execution permanently failed for {event.symbol_request} {event.timeframe} "
                    f"{event.signal_source}: {exc}"
                )
                self.pending_execution_events.pop(event_id, None)
                image_path = event.image_path
                if image_path is None:
                    image_path = save_signal_chart(result, event, self.config)
                    event.image_path = image_path
                self.finish_event(event, Path(image_path))
                self.pending_confirmation_attempts.pop(event_id, None)
            except Exception as exc:
                print(
                    f"Pending live execution failed for {event.symbol_request} {event.timeframe} "
f"{event.signal_source}: {exc}"
                )

    def _cleanup_stale_opposite_pending_ids(self) -> None:
        active_pending_ids = set(self.pending_execution_events.keys())
        changed = False
        for tracker in self.open_positions.values():
            stale_ids = set(getattr(tracker, "opposite_pending_closed", set()))
            valid_ids = stale_ids & active_pending_ids
            if valid_ids != stale_ids:
                tracker.opposite_pending_closed = valid_ids
                changed = True
        if changed:
            self.save_position_trackers()

    def _execute_order_if_enabled(self, event: SignalEvent, mt5_client: MT5Client, raw_overrides: dict[str, tuple[float, str]] | None = None, entry_buffer_override: dict[str, tuple[float, str]] | None = None) -> ExecutionStatus:
        # Apply any absolute raw overrides (pips/points/price/percent) to event levels before validating
        signal_price = event.entry_price if event.entry_price is not None else event.price
        if raw_overrides:
            self._apply_raw_overrides_to_event(event, mt5_client, raw_overrides)
        # ensure event.take_profit is set (fallback to tp1)
        if event.take_profit is None:
            event.take_profit = event.take_profit_1 if getattr(event, "take_profit_1", None) is not None else event.take_profit

        if not event.stop_loss or not event.take_profit:
            raise RuntimeError("Cannot execute order without both stop-loss and take-profit levels.")

        if self._has_local_open_position(event.symbol_actual):
            print(
                f"Skipping live execution for {event.symbol_actual} because this watcher already tracks an open position."
            )
            return "skipped"

        # Allow opposite-direction same-symbol entries to proceed independently.
        # Only block another pending entry in the same direction for the same symbol.
        if self._has_pending_execution_for_symbol_direction(
            event.symbol_actual,
            event.zone.direction,
            exclude_event_id=event.event_id,
        ):
            print(
                f"Skipping live execution for {event.symbol_actual} because another entry in the same direction is already pending for this pair."
            )
            return "skipped"

        if self._has_pending_execution_for_symbol_direction(
            event.symbol_actual,
            None,
            exclude_event_id=event.event_id,
        ):
            print(
                f"Accepting opposite-direction live execution for {event.symbol_actual} while a pending opposite-side entry exists."
            )

        try:
            has_position = mt5_client.has_open_position(event.symbol_actual)
        except Exception as exc:
            has_position = False
            print(f"Warning: failed to query open positions for {event.symbol_actual}: {exc}")

        if has_position:
            print(
                f"Skipping live execution for {event.symbol_actual} because an open position already exists. has_open_position={has_position}"
            )
            return "skipped"

        try:
            tradeable = mt5_client.is_symbol_tradeable(event.symbol_actual)
        except Exception as exc:
            tradeable = False
            print(f"Warning: failed to determine tradeable state for {event.symbol_actual}: {exc}")

        if not tradeable:
            print(f"Live execution disabled because symbol {event.symbol_actual} is not tradeable. tradeable={tradeable}")
            raise RuntimeError(
                f"Live execution disabled because symbol {event.symbol_actual} is not tradeable."
            )

        # Check if price has moved beyond the required entry threshold in the signal direction
        tick = mt5_client.get_tick(event.symbol_actual)
        symbol_info: Any = mt5_client.get_symbol_market_status(event.symbol_actual)
        # Diagnostic log to help explain why a trade may not have been placed
        try:
            point_val = symbol_info.get("point", 0.0001) if isinstance(symbol_info, dict) else getattr(symbol_info, "point", 0.0001)
            point = self._as_optional_float(point_val) or 0.0001
        except Exception:
            point = 0.0001
        print(
            f"Debug MT5 state for {event.symbol_actual}: has_position={has_position}, tradeable={tradeable}, tick_ask={getattr(tick,'ask',None)}, tick_bid={getattr(tick,'bid',None)}, point={point}"
        )
        digits = self._as_optional_int(symbol_info.get("digits") if isinstance(symbol_info, dict) else getattr(symbol_info, "digits", None))
        # Use entry_price if available, otherwise use signal price
        signal_price = event.entry_price if event.entry_price is not None else event.price
        required_price, max_price = self._get_entry_buffer_prices(
            signal_price,
            float(point),
            event.zone.direction,
            entry_buffer_override,
            digits,
        )

        price_tolerance = float(point) * 2
        float_epsilon = 1e-12
        if event.zone.direction == "bullish":
            current_price = float(tick.ask)
            if current_price < required_price:
                print(
                    f"Waiting for bullish entry: price {current_price:.5f} < required {required_price:.5f} "
                    f"({self._format_entry_buffer(entry_buffer_override)})"
                )
                return "pending"
            if (
                max_price is not None
                and current_price - max_price > price_tolerance + float_epsilon
            ):
                print(
                    f"Waiting for bullish entry: price {current_price:.5f} > max allowed {max_price:.5f} "
                    f"(+{price_tolerance:.5f} tolerance, {self._format_entry_buffer(entry_buffer_override)})"
                )
                return "pending"
        else:  # bearish
            current_price = float(tick.bid)
            if current_price > required_price:
                print(
                    f"Waiting for bearish entry: price {current_price:.5f} > required {required_price:.5f} "
                    f"({self._format_entry_buffer(entry_buffer_override)})"
                )
                return "pending"
            if (
                max_price is not None
                and max_price - current_price > price_tolerance + float_epsilon
            ):
                print(
                    f"Waiting for bearish entry: price {current_price:.5f} < max allowed {max_price:.5f} "
                    f"(-{price_tolerance:.5f} tolerance, {self._format_entry_buffer(entry_buffer_override)})"
                )
                return "pending"

        # --- RISK MANAGEMENT GATES ---
        # Keep execution simple: only block when the market is clearly not tradable or the signal is
        # still below the required price threshold. This avoids missing valid entries due to extra gating.
        if not self._check_daily_loss_breaker(mt5_client):
            print(
                f"Skipping execution for {event.symbol_actual} because the daily loss breaker is active."
            )
            return "skipped"
        if not self._check_correlation_cap(event.symbol_actual):
            print(
                f"Skipping execution for {event.symbol_actual} because the correlation cap is active."
            )
            return "skipped"
        if not self._check_news_blackout(event.symbol_actual):
            print(
                f"Skipping execution for {event.symbol_actual} because the news blackout is active."
            )
            return "skipped"

        if not self._is_trade_likely_profitable(event, mt5_client, tick, float(point)):
            print(
                f"Skipping execution for {event.symbol_actual} because the trade did not pass profitability gating."
            )
            return "skipped"

        quality_score = self._score_signal_quality(event, mt5_client, tick, float(point))
        min_quality = getattr(self.config, "min_signal_quality_score", 0)
        print(f"[Quality] {event.symbol_actual}: score={quality_score:.1f}/100 (min threshold: {min_quality})")
        if quality_score < min_quality:
            print(
                f"Skipping execution for {event.symbol_actual}: signal quality {quality_score:.1f} < threshold {min_quality}"
            )
            return "skipped"

        equity_volume = self._calculate_equity_based_volume(event, mt5_client)
        volume = equity_volume if equity_volume is not None else float(self.config.order_volume)
        
        # M1 trades use fixed 0.02 lots
        if event.timeframe == "M1":
            volume = 0.02
            print(f"M1 timeframe: using fixed volume 0.02 lots")
        elif getattr(self.config, "reduce_position_size", False):
            fraction = float(getattr(self.config, "reduce_position_fraction", 0.5))
            volume = max(0.0, volume * fraction)

        normalized_volume = mt5_client.normalize_order_volume(event.symbol_actual, volume)
        if normalized_volume != volume:
            print(
                f"Normalized order volume for {event.symbol_actual}: requested={volume:.3f}, normalized={normalized_volume:.3f}"
            )
        else:
            print(
                f"Order volume for {event.symbol_actual} is already normalized: {normalized_volume:.3f}"
            )

        if normalized_volume <= 0.0:
            raise RuntimeError(
                f"Cannot execute order for {event.symbol_actual}: normalized volume {normalized_volume} is invalid."
            )

        ticket = mt5_client.place_market_order(
            event.symbol_actual,
            event.zone.direction,
            normalized_volume,
            stop_loss=event.stop_loss,
            take_profit=event.take_profit,
            deviation=self.config.order_deviation,
            magic=self.config.order_magic_number,
        )
        event.order_ticket = ticket
        print(
            f"Placed live order {ticket} for {event.symbol_actual} {event.zone.direction} "
            f"entry={signal_price:.5f} stop_loss={event.stop_loss:.5f} take_profit={event.take_profit:.5f}"
        )

        # Send alert for live trade execution
        try:
            tf_name = getattr(event, "timeframe_name", None) or getattr(event, "timeframe", "Unknown")
            trade_alert = (
                f"🟢 LIVE TRADE OPENED\n"
                f"Symbol: {event.symbol_request} ({event.symbol_actual})\n"
                f"Direction: {event.zone.direction.upper()}\n"
                f"Ticket: {ticket}\n"
                f"Entry: {signal_price:.5f}\n"
                f"Volume: {normalized_volume:.3f}\n"
                f"Stop Loss: {event.stop_loss:.5f}\n"
                f"Take Profit: {event.take_profit:.5f}\n"
                f"Timeframe: {tf_name}"
            )
            self.notifier.send_text_alert(trade_alert)
        except Exception as exc:
            print(f"Failed to send trade execution alert: {exc}")

        # Track position for TP-based partial closes (support 2-TP and 3-TP configs)
        if event.take_profit_1 and event.take_profit_2:
            executed_volume = normalized_volume
            tracker_entry_price = float(signal_price)
            try:
                for position in mt5_client.get_open_positions(event.symbol_actual):
                    if int(getattr(position, "ticket", 0)) == int(ticket):
                        tracker_entry_price = float(getattr(position, "price_open"))
                        break
            except Exception:
                pass

            # NASDAQ-specific: take 60% at TP1, 40% at TP2, no runner to TP3
            # NASDAQ reverses sharply — don't hold a runner hoping for TP3
            canonical = (event.symbol_request or "").upper().rstrip("mM").rstrip(".")
            is_nasdaq = any(alias in canonical for alias in ["NASDAQ", "NAS100", "USTEC", "US100", "NAS"])
            if event.take_profit_3 is not None:
                # 3-TP configuration
                if is_nasdaq:
                    tp1_pct, tp2_pct, tp3_pct = 60.0, 40.0, 0.0
                else:
                    tp1_pct, tp2_pct, tp3_pct = 50.0, 30.0, 20.0
            else:
                # 2-TP configuration: distribute into TP1/TP2 (no runner)
                tp1_pct, tp2_pct, tp3_pct = 60.0, 40.0, 0.0

            tp1_qty, tp2_qty, tp3_qty = self._normalize_partial_close_volumes(
                mt5_client,
                event.symbol_actual,
                executed_volume,
                tp1_pct,
                tp2_pct,
                tp3_pct,
            )

            tracker = PositionTracker(
                ticket=ticket,
                symbol=event.symbol_actual,
                timeframe=event.timeframe,
                direction=event.zone.direction,
                entry_price=tracker_entry_price,
                volume=executed_volume,
                stop_loss=float(event.stop_loss),
                take_profit_1=float(event.take_profit_1),
                take_profit_2=float(event.take_profit_2),
                take_profit_3=(float(event.take_profit_3) if event.take_profit_3 is not None else None),
                tp1_qty=tp1_qty,
                tp2_qty=tp2_qty,
                tp3_qty=tp3_qty,
            )
            self.open_positions[ticket] = tracker
            self.save_position_trackers()
            if event.take_profit_3 is not None:
                print(
                    f"Tracking position {ticket} for partial closes: "
                    f"TP1({tp1_qty:.3f}@{event.take_profit_1:.5f}) "
                    f"TP2({tp2_qty:.3f}@{event.take_profit_2:.5f}) "
                    f"TP3({tp3_qty:.3f}@{event.take_profit_3:.5f})"
                )
            else:
                print(
                    f"Tracking position {ticket} for partial closes: "
                    f"TP1({tp1_qty:.3f}@{event.take_profit_1:.5f}) "
                    f"TP2({tp2_qty:.3f}@{event.take_profit_2:.5f}) (no TP3)"
                )
        return "placed"

    def _filter_redundant_swift_events(
        self,
        symbol_request: str,
        symbol_actual: str,
        timeframe_name: str,
        events: list[SignalEvent],
    ) -> list[SignalEvent]:
        key = (symbol_actual, timeframe_name)
        previous_ids = self.last_swift_event_ids.get(key, set())
        current_ids = {event.event_id for event in events}
        self.last_swift_event_ids[key] = current_ids
        return [event for event in events if event.event_id not in previous_ids]

    @staticmethod
    def _as_optional_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_optional_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            result = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        try:
            import math

            if not math.isfinite(result):
                return None
        except (TypeError, ValueError):
            return None
        return result

    @staticmethod
    def _pip_size(point: float, digits: int | None = None) -> float:
        if digits in (3, 5):
            return float(point) * 10.0
        return float(point)

    @classmethod
    def _convert_price_delta(
        cls,
        signal_price: float,
        value: float,
        unit: str,
        point: float,
        digits: int | None = None,
    ) -> float:
        unit = unit.lower()
        if unit == "percent":
            return float(signal_price) * (float(value) / 100.0)
        if unit in ("pips", "pip"):
            return float(value) * cls._pip_size(point, digits)
        if unit in ("points", "pts"):
            return float(value)
        if unit == "price":
            return float(value)
        raise ValueError(f"Unsupported price delta unit: {unit}")

    def _get_entry_buffer_prices(
        self,
        signal_price: float,
        point: float,
        direction: str,
        entry_buffer_override: dict[str, tuple[float, str]] | None = None,
        digits: int | None = None,
    ) -> tuple[float, float | None]:
        if entry_buffer_override is not None:
            min_value, min_unit = entry_buffer_override["min"]
            max_value, max_unit = entry_buffer_override["max"]
            min_delta = self._convert_price_delta(signal_price, min_value, min_unit, point, digits)
            max_delta = self._convert_price_delta(signal_price, max_value, max_unit, point, digits)
            if direction == "bullish":
                return float(signal_price) + min_delta, float(signal_price) + max_delta
            return float(signal_price) - min_delta, float(signal_price) - max_delta

        delta = self.config.entry_pip_buffer * self._pip_size(point, digits)
        if direction == "bullish":
            return float(signal_price) + delta, None
        return float(signal_price) - delta, None

    def _sync_event_take_profit(self, event: SignalEvent) -> None:
        configured_target = str(getattr(self.config, "tp_target_level", "tp3") or "tp3").strip().lower()
        if configured_target in {"tp1", "tp2", "tp3"}:
            target_levels = [configured_target]
            for level in ("tp1", "tp2", "tp3"):
                if level not in target_levels:
                    target_levels.append(level)
            for level in target_levels:
                value = getattr(event, f"take_profit_{level[-1]}", None)
                if value is not None:
                    event.take_profit = value
                    return

        label = event.signal_label.lower()
        if "tp1" in label and event.take_profit_1 is not None:
            event.take_profit = event.take_profit_1
        elif "tp2" in label and event.take_profit_2 is not None:
            event.take_profit = event.take_profit_2
        elif event.take_profit_3 is not None:
            event.take_profit = event.take_profit_3
        elif event.take_profit_2 is not None:
            event.take_profit = event.take_profit_2
        elif event.take_profit_1 is not None:
            event.take_profit = event.take_profit_1
        elif event.take_profit is not None:
            event.take_profit = event.take_profit

    def _apply_raw_overrides_to_event(
        self,
        event: SignalEvent,
        mt5_client: MT5Client,
        raw_overrides: dict[str, tuple[float, str]],
    ) -> None:
        signal_price = float(event.entry_price if event.entry_price is not None else event.price)
        symbol_info: Any = mt5_client.get_symbol_market_status(event.symbol_actual)
        point_val = symbol_info.get("point", 0.0001) if isinstance(symbol_info, dict) else getattr(symbol_info, "point", 0.0001)
        point = self._as_optional_float(point_val) or 0.0001
        digits = self._as_optional_int(symbol_info.get("digits") if isinstance(symbol_info, dict) else getattr(symbol_info, "digits", None))
        expected_prefix = "main_"

        for prefix, (value, unit) in raw_overrides.items():
            if not prefix.startswith(expected_prefix):
                continue
            try:
                delta = self._convert_price_delta(
                    signal_price,
                    float(value),
                    unit,
                    point,
                    digits,
                )
            except ValueError:
                continue

            if prefix.endswith("tp1"):
                event.take_profit_1 = (
                    signal_price + delta if event.zone.direction == "bullish" else signal_price - delta
                )
            elif prefix.endswith("tp2"):
                event.take_profit_2 = (
                    signal_price + delta if event.zone.direction == "bullish" else signal_price - delta
                )
            elif prefix.endswith("tp3"):
                event.take_profit_3 = (
                    signal_price + delta if event.zone.direction == "bullish" else signal_price - delta
                )
            elif prefix.endswith("sl"):
                event.stop_loss = (
                    signal_price - delta if event.zone.direction == "bullish" else signal_price + delta
                )

        self._sync_event_take_profit(event)
        self._sync_event_risk_reward(event)
        self._sync_event_zone_bounds(event)

    def _sync_event_risk_reward(self, event: SignalEvent) -> None:
        if event.entry_price is None or event.stop_loss is None or event.take_profit_3 is None:
            return
        if event.zone.direction == "bullish":
            risk = float(event.entry_price) - float(event.stop_loss)
            reward = float(event.take_profit_3) - float(event.entry_price)
        else:
            risk = float(event.stop_loss) - float(event.entry_price)
            reward = float(event.entry_price) - float(event.take_profit_3)
        event.risk_reward_ratio = float(reward / risk) if risk > 0 else None

    def _sync_event_zone_bounds(self, event: SignalEvent) -> None:
        prices = [
            value
            for value in (
                event.entry_price,
                event.stop_loss,
                event.take_profit_1,
                event.take_profit_2,
                event.take_profit_3,
            )
            if value is not None
        ]
        if prices:
            event.zone.top = float(max(prices))
            event.zone.bottom = float(min(prices))

    def _has_local_open_position(self, symbol: str) -> bool:
        return any(tracker.symbol == symbol for tracker in self.open_positions.values())

    def _normalize_timeframe(self, timeframe: str) -> str:
        # Accept strings or numeric timeframe codes (e.g. mt5 constants)
        if isinstance(timeframe, int):
            # try reverse lookup from TIMEFRAME_MAP values to keys
            rev = {v: k for k, v in TIMEFRAME_MAP.items()}
            if timeframe in rev:
                return rev[timeframe]
            return str(timeframe)

        normalized = str(timeframe).strip().upper()
        aliases = {
            "15M": "M15",
            "M15": "M15",
            "30M": "M30",
            "M30": "M30",
            "1H": "H1",
            "H1": "H1",
            "4H": "H4",
            "H4": "H4",
            "1D": "D1",
            "D1": "D1",
            "D": "D1",
        }
        if normalized in aliases:
            return aliases[normalized]
        # Accept bare numbers like '15' or '60'
        if normalized.isdigit():
            n = int(normalized)
            if n == 15:
                return "M15"
            if n == 30:
                return "M30"
            if n == 60:
                return "H1"
            if n == 240:
                return "H4"
            if n == 1:
                return "D1"
        # fallback: return as-is
        return normalized

    def _parent_bias_timeframe(self, timeframe: str) -> str | None:
        """Return the parent timeframe used for bias alignment checks."""
        normalized = self._normalize_timeframe(timeframe)
        mapping = {
            "M1": "M15",
            "M5": "M30",
            "M15": "H1",
            "M30": "H4",
            "H1": "D1",
            "H4": "D1",
        }
        return mapping.get(normalized)

    def _get_htf_bias(self, symbol: str, mt5_client: MT5Client, timeframe: str = "H4") -> str | None:
        """Return 'bullish' or 'bearish' based on market structure on the given timeframe.
        Returns None if unable to determine."""
        try:
            candles = mt5_client.get_recent_candles(symbol, timeframe, 20)
            if len(candles) < 5:
                return None
            # Simple HH/HL vs LH/LL structure check
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            mid = len(highs) // 2
            early_high = max(highs[:mid])
            recent_high = max(highs[mid:])
            early_low = min(lows[:mid])
            recent_low = min(lows[mid:])
            if recent_high > early_high and recent_low > early_low:
                return "bullish"   # HH + HL = uptrend
            if recent_high < early_high and recent_low < early_low:
                return "bearish"   # LH + LL = downtrend
            return None  # ranging — no clear bias
        except Exception:
            return None

    def _should_close_near_stop_loss(
        self,
        tracker: PositionTracker,
        current_price: float,
        mt5_client: MT5Client,
        point: float,
    ) -> bool:
        """Return True when an open position is dangerously close to SL and invalidated."""
        if not self.config.stop_loss_proximity_exit_enabled:
            return False

        symbol_key = self._canonical_symbol_key(tracker.symbol)
        if symbol_key and symbol_key not in self.config.stop_loss_proximity_exit_symbols:
            return False

        normalized_tf = self._normalize_timeframe(tracker.timeframe)
        if normalized_tf not in self.config.stop_loss_proximity_exit_timeframes:
            return False

        if tracker.tp3_closed:
            return False
        remaining = tracker.get_remaining_volume()
        if remaining <= 0:
            return False

        sl_distance = abs(float(tracker.entry_price) - float(tracker.stop_loss))
        if sl_distance <= 0:
            return False

        current_distance = abs(current_price - float(tracker.stop_loss))
        threshold = sl_distance * self.config.stop_loss_proximity_exit_ratio
        if current_distance > threshold:
            return False

        direction = tracker.direction
        entry_price = float(tracker.entry_price)
        if direction == "bullish":
            wrong_side = current_price <= entry_price
            opposite_direction = "bearish"
        else:
            wrong_side = current_price >= entry_price
            opposite_direction = "bullish"

        if wrong_side:
            return True

        parent_tf = self._parent_bias_timeframe(tracker.timeframe)
        bias = self._get_htf_bias(tracker.symbol, mt5_client, parent_tf or "H4")
        return bias == opposite_direction

    # ------------------------------------------------------------------
    # Risk management checks
    # ------------------------------------------------------------------

    def _check_daily_loss_breaker(self, mt5_client: MT5Client) -> bool:
        """Return True if trading should continue. Returns False if daily loss limit hit (pause trading).
        Resets the daily start equity automatically at the start of a new day."""
        if not self.config.daily_loss_breaker_enabled:
            return True

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.daily_start_date != today_str:
            # New day — reset tracking
            equity = self._as_optional_float(mt5_client.get_account_equity())
            self.daily_start_equity = equity
            self.daily_start_date = today_str
            self.trading_paused_for_day = False
            if equity is not None:
                print(f"[RiskMgmt] New trading day. Starting equity: {equity:.2f}")

        if self.trading_paused_for_day:
            return False

        if self.daily_start_equity is None or self.daily_start_equity <= 0:
            return True  # can't evaluate, don't block

        current_equity = self._as_optional_float(mt5_client.get_account_equity())
        if current_equity is None:
            return True

        loss_percent = ((self.daily_start_equity - current_equity) / self.daily_start_equity) * 100.0
        if loss_percent >= self.config.daily_max_loss_percent:
            self.trading_paused_for_day = True
            print(
                f"[RiskMgmt] DAILY LOSS LIMIT HIT: -{loss_percent:.2f}% "
                f"(limit {self.config.daily_max_loss_percent}%). Trading paused until tomorrow."
            )
            return False
        return True

    def _check_correlation_cap(self, symbol: str) -> bool:
        """Return True if a new position on `symbol` is allowed under the correlation exposure cap."""
        canonical = (symbol or "").upper().rstrip("mM").rstrip(".")
        for group in self.config.correlation_groups:
            group_upper = [g.upper() for g in group]
            matched = any(canonical.startswith(g) or g.startswith(canonical) for g in group_upper)
            if not matched:
                continue
            # Count currently open positions whose symbol falls in this group
            open_in_group = 0
            for tracker in self.open_positions.values():
                t_canonical = tracker.symbol.upper().rstrip("mM").rstrip(".")
                if any(t_canonical.startswith(g) or g.startswith(t_canonical) for g in group_upper):
                    open_in_group += 1
            if open_in_group >= self.config.max_correlated_open_positions:
                print(
                    f"[RiskMgmt] Blocked {symbol}: correlation cap reached for group {group} "
                    f"({open_in_group}/{self.config.max_correlated_open_positions} already open)"
                )
                return False
        return True

    def _check_news_blackout(self, symbol: str) -> bool:
        """Return True if trading is allowed (no news blackout active).
        Uses a lightweight static high-impact news calendar check; integrate a real
        calendar feed (e.g. ForexFactory/Finnhub) for production use."""
        if not self.config.news_blackout_enabled:
            return True
        # Placeholder hook: self.upcoming_news_events should be populated by an
        # external calendar fetch. If not populated, this check passes through.
        events = getattr(self, "upcoming_news_events", None)
        if not events:
            return True
        now = datetime.now(timezone.utc)
        canonical = (symbol or "").upper().rstrip("mM").rstrip(".")
        for event in events:
            if len(event) == 3:
                event_time, affected_symbols, metadata = event
            else:
                event_time, affected_symbols = event
                metadata = {}
            affected_upper = [s.upper() for s in affected_symbols]
            if not any(canonical.startswith(s) or s.startswith(canonical) for s in affected_upper):
                continue
            delta_minutes = abs((event_time - now).total_seconds()) / 60.0
            window = max(self.config.news_blackout_minutes_before, self.config.news_blackout_minutes_after)
            if delta_minutes <= window:
                event_label = metadata.get("event") or metadata.get("currency") or "high-impact news"
                print(
                    f"[RiskMgmt] Blocked {symbol}: inside news blackout window for {event_label} at {event_time.isoformat()}"
                )
                return False
        return True

    def _calculate_equity_based_volume(self, event, mt5_client: MT5Client) -> float | None:
        """Return position size based on % risk of current equity, or None to fall back to fixed volume."""
        if not self.config.use_equity_based_sizing:
            return None
        equity = self._as_optional_float(mt5_client.get_account_equity())
        if equity is None or equity <= 0:
            return None
        try:
            sl_distance = abs(float(event.entry_price or event.price) - float(event.stop_loss))
            if sl_distance <= 0:
                return None
            risk_amount = equity * (self.config.risk_percent_per_trade / 100.0)
            # NOTE: pip/point value per lot varies by symbol & broker — this is a simplified
            # calculation assuming 1 lot ≈ $1 per point for indices, or standard pip value for forex.
            # For production accuracy, fetch symbol_info().trade_tick_value from MT5.
            tick_value = 1.0
            try:
                import MetaTrader5 as mt5  # local import to avoid hard dependency at module load
                symbol_info_fn = getattr(mt5, "symbol_info", None)
                if callable(symbol_info_fn):
                    info = symbol_info_fn(event.symbol_actual)
                    if info is not None and getattr(info, "trade_tick_value", None):
                        tick_value = float(getattr(info, "trade_tick_value"))
            except Exception:
                pass
            volume = risk_amount / (sl_distance * max(tick_value, 0.0001))
            return round(max(volume, 0.01), 2)
        except Exception:
            return None

    def check_weekend_flatten(self, mt5_client: MT5Client) -> None:
        """Flatten crypto positions before the weekend gap if enabled. Call this once per scan loop."""
        if not self.config.weekend_flatten_enabled:
            return
        now = datetime.now(timezone.utc)
        is_flatten_window = (
            now.weekday() == self.config.weekend_flatten_day_gmt
            and now.hour >= self.config.weekend_flatten_hour_gmt
        )
        if not is_flatten_window:
            self.weekend_flatten_done_this_week = False
            return
        if self.weekend_flatten_done_this_week:
            return

        targets = [s.upper() for s in self.config.weekend_flatten_symbols]
        for ticket, tracker in list(self.open_positions.items()):
            canonical = tracker.symbol.upper().rstrip("mM").rstrip(".")
            if any(canonical.startswith(t) for t in targets):
                ok = mt5_client.close_position_full(ticket)
                if ok:
                    print(f"[RiskMgmt] Weekend flatten: closed {tracker.symbol} position {ticket}")
                    # Send alert for position close
                    try:
                        close_alert = (
                            f"⚠️ POSITION CLOSED - WEEKEND FLATTEN\n"
                            f"Symbol: {tracker.symbol}\n"
                            f"Direction: {tracker.direction.upper()}\n"
                            f"Ticket: {ticket}\n"
                            f"Reason: Weekend risk management"
                        )
                        self.notifier.send_text_alert(close_alert)
                    except Exception as exc:
                        print(f"Failed to send close alert: {exc}")
                    self.open_positions.pop(ticket, None)
                else:
                    print(f"[RiskMgmt] Weekend flatten: FAILED to close {tracker.symbol} position {ticket}")
        self.weekend_flatten_done_this_week = True

    def check_news_flatten(self, mt5_client: MT5Client) -> None:
        """Flatten open positions shortly before a scheduled high-impact news event that
        affects their symbol.

        _check_news_blackout() only blocks NEW entries near an event -- it does nothing
        for a position that was already opened earlier (e.g. hours before) and is still
        open when the news actually hits. This closes that exposure ahead of the event
        instead of leaving it to absorb the volatility spike.
        """
        if not self.config.news_flatten_enabled:
            return
        events = getattr(self, "upcoming_news_events", None)
        if not events:
            return

        now = datetime.now(timezone.utc)
        window = self.config.news_flatten_minutes_before

        for ticket, tracker in list(self.open_positions.items()):
            canonical = tracker.symbol.upper().rstrip("mM").rstrip(".")
            for event in events:
                if len(event) == 3:
                    event_time, affected_symbols, metadata = event
                else:
                    event_time, affected_symbols = event
                    metadata = {}
                affected_upper = [s.upper() for s in affected_symbols]
                if not any(canonical.startswith(s) or s.startswith(canonical) for s in affected_upper):
                    continue

                minutes_until_event = (event_time - now).total_seconds() / 60.0
                # Only act while the event is still ahead of us, inside the flatten
                # window. Ignore events already in the past (or too far in the future).
                if not (0 <= minutes_until_event <= window):
                    continue

                event_key = (event_time.isoformat(), canonical)
                if event_key in self.news_flattened_events:
                    continue

                ok = mt5_client.close_position_full(ticket)
                if ok:
                    print(
                        f"[RiskMgmt] News flatten: closed {tracker.symbol} position {ticket} "
                        f"ahead of high-impact event at {event_time.isoformat()}"
                    )
                    # Send alert for position close
                    try:
                        close_alert = (
                            f"⚠️ POSITION CLOSED - NEWS FLATTEN\n"
                            f"Symbol: {tracker.symbol}\n"
                            f"Direction: {tracker.direction.upper()}\n"
                            f"Ticket: {ticket}\n"
                            f"Reason: High-impact news event at {event_time.isoformat()}"
                        )
                        self.notifier.send_text_alert(close_alert)
                    except Exception as exc:
                        print(f"Failed to send close alert: {exc}")
                    self.open_positions.pop(ticket, None)
                else:
                    print(
                        f"[RiskMgmt] News flatten: FAILED to close {tracker.symbol} position "
                        f"{ticket} ahead of event at {event_time.isoformat()}"
                    )
                self.news_flattened_events.add(event_key)
                break  # this ticket is handled (closed or attempted); move to the next one

    def _is_trade_likely_profitable(self, event, mt5_client: MT5Client, tick, point: float) -> bool:
        """Pre-execution gate — only place trade if 5 conditions are met.
        Returns True if trade should proceed, False to block it."""

        symbol = event.symbol_actual
        direction = event.zone.direction

        # --- CHECK 1: Spread must be < 30% of SL distance ---
        try:
            spread = float(tick.ask) - float(tick.bid)
            sl_distance = abs(float(event.stop_loss) - float(event.entry_price or event.price))
            if sl_distance > 0 and spread > (sl_distance * 0.30):
                print(
                    f"[Gate] Blocked {symbol}: spread {spread:.5f} > 30% of SL distance {sl_distance:.5f}"
                )
                return False
        except Exception as exc:
            print(f"[Gate] Spread check failed for {symbol}: {exc}")
            return False

        # --- CHECK 2: Last 3 M15 candles must show at least some momentum in signal direction ---
        try:
            candles = mt5_client.get_recent_candles(symbol, "M15", 3)
            if len(candles) >= 2:
                bullish_count = sum(1 for c in candles if c["close"] > c["open"])
                if direction == "bullish" and bullish_count < 1:
                    print(
                        f"[Gate] Blocked {symbol}: weak bullish momentum ({bullish_count}/3 bullish candles)"
                    )
                    return False
                if direction == "bearish" and bullish_count > 2:
                    print(
                        f"[Gate] Blocked {symbol}: weak bearish momentum ({bullish_count}/3 bearish candles)"
                    )
                    return False
        except Exception as exc:
            print(f"[Gate] Momentum check failed for {symbol}: {exc}")
            return False

        # --- CHECK 4: Last closed candle must show body > 40% of total range (commitment) ---
        try:
            candles = mt5_client.get_recent_candles(symbol, "M15", 1)
            if candles:
                c = candles[-1]
                body = abs(c["close"] - c["open"])
                total_range = c["high"] - c["low"]
                if total_range > 0 and (body / total_range) < 0.40:
                    print(
                        f"[Gate] Blocked {symbol}: last candle body ratio {body/total_range:.2f} < 0.40 (wick-dominated, no commitment)"
                    )
                    return False
        except Exception as exc:
            print(f"[Gate] Candle body check failed for {symbol}: {exc}")
            return False

        # --- CHECK 5: TP1 path must not have a major opposing OB/swing in the way ---
        # Allow trades even if price has moved far along the TP1 path (disabled late-entry block)
        try:
            entry = float(event.entry_price or event.price)
            tp1 = float(event.take_profit_1)
            current = float(tick.ask) if direction == "bullish" else float(tick.bid)
            move_to_tp1 = abs(tp1 - entry)
            already_moved = abs(current - entry)
            # Threshold set to 100% to allow trades regardless of how far price has moved
            if move_to_tp1 > 0 and (already_moved / move_to_tp1) > 1.00:
                print(
                    f"[Gate] Blocked {symbol}: price already past TP1 — entry is impossible"
                )
                return False
        except Exception as exc:
            print(f"[Gate] TP1 path check failed for {symbol}: {exc}")
            return False

        print(f"[Gate] {symbol} passed all 5 pre-execution checks — placing trade")
        return True

    def _score_signal_quality(
        self,
        event: SignalEvent,
        mt5_client: MT5Client,
        tick: Any,
        point: float,
    ) -> float:
        """Calculate signal quality score (0-100) based on confluence of gate metrics.
        
        Uses the same data from execution gates to score:
        - Momentum: bullish/bearish candle alignment (0-20 pts)
        - H4 bias: HTF structure alignment (0-15 pts)
        - Entry freshness: TP1 path not exhausted (0-10 pts)
        - Candle commitment: body > 40% of range (0-10 pts)
        - Spread efficiency: spread < 30% of SL (0-10 pts)
        - Base score: 35 pts
        
        Total: 0-100. Use min_signal_quality_score to filter weak signals.
        """
        score = 35.0  # baseline
        symbol = event.symbol_actual
        direction = event.zone.direction

        # 1. MOMENTUM SCORE (0-20)
        try:
            candles = mt5_client.get_recent_candles(symbol, "M15", 3)
            if len(candles) >= 2:
                bullish_count = sum(1 for c in candles if c["close"] > c["open"])
                if direction == "bullish":
                    if bullish_count >= 2:
                        score += 20  # strong
                    elif bullish_count == 1:
                        score += 10  # moderate
                    # bullish_count == 0 adds 0 (weak)
                else:  # bearish
                    bearish_count = len(candles) - bullish_count
                    if bearish_count >= 2:
                        score += 20  # strong
                    elif bearish_count == 1:
                        score += 10  # moderate
        except Exception:
            pass

        # 2. HTF BIAS SCORE (0-15)
        try:
            parent_tf = self._parent_bias_timeframe(event.timeframe)
            if parent_tf is not None:
                htf_bias = self._get_htf_bias(symbol, mt5_client, timeframe=parent_tf)
                if htf_bias == direction:
                    score += 15  # strong alignment
                elif htf_bias is not None:
                    score += 5  # opposite bias, but we proceeded anyway (caution)
        except Exception:
            pass

        # 3. ENTRY FRESHNESS SCORE (0-10)
        try:
            entry = float(event.entry_price or event.price)
            tp1 = event.take_profit_1
            if tp1 is not None:
                tp1 = float(tp1)
                current = float(tick.ask) if direction == "bullish" else float(tick.bid)
                move_to_tp1 = abs(tp1 - entry)
                already_moved = abs(current - entry)
                if move_to_tp1 > 0:
                    exhaustion_ratio = already_moved / move_to_tp1
                    if exhaustion_ratio < 0.2:
                        score += 10  # fresh entry
                    elif exhaustion_ratio < 0.35:
                        score += 7   # moderately fresh
                    elif exhaustion_ratio < 0.50:
                        score += 3   # somewhat late
        except Exception:
            pass

        # 4. CANDLE COMMITMENT SCORE (0-10)
        try:
            candles = mt5_client.get_recent_candles(symbol, "M15", 1)
            if candles:
                c = candles[-1]
                body = abs(c["close"] - c["open"])
                total_range = c["high"] - c["low"]
                if total_range > 0:
                    body_ratio = body / total_range
                    if body_ratio > 0.60:
                        score += 10  # strong commitment
                    elif body_ratio > 0.50:
                        score += 7   # good commitment
                    elif body_ratio > 0.40:
                        score += 4   # acceptable
        except Exception:
            pass

        # 5. SPREAD EFFICIENCY SCORE (0-10)
        try:
            spread = float(tick.ask) - float(tick.bid)
            sl = event.stop_loss
            entry_price = event.entry_price or event.price
            if sl is not None and entry_price is not None:
                sl_distance = abs(float(sl) - float(entry_price))
                if sl_distance > 0:
                    spread_ratio = spread / sl_distance
                    if spread_ratio < 0.10:
                        score += 10  # tight spread
                    elif spread_ratio < 0.20:
                        score += 7   # good spread
                    elif spread_ratio < 0.30:
                        score += 3   # acceptable spread
        except Exception:
            pass

        return min(100.0, max(0.0, score))

    def _has_pending_execution_for_symbol_direction(
        self,
        symbol: str,
        direction: str | None = None,
        exclude_event_id: str | None = None,
    ) -> bool:
        for event_id, (pending_event, _, _) in self.pending_execution_events.items():
            if event_id == exclude_event_id:
                continue
            if pending_event.symbol_actual != symbol:
                continue
            if direction is None or pending_event.zone.direction == direction:
                return True
        return False

    @staticmethod
    def _format_entry_buffer(entry_buffer_override: dict[str, tuple[float, str]] | None) -> str:
        if entry_buffer_override is None:
            return "global entry_pip_buffer"
        min_value, min_unit = entry_buffer_override["min"]
        max_value, max_unit = entry_buffer_override["max"]
        return f"{min_value}{min_unit}-{max_value}{max_unit}"

    def _normalize_partial_close_volumes(
        self,
        mt5_client: MT5Client,
        symbol: str,
        executed_volume: float,
        tp1_percent: float,
        tp2_percent: float,
        tp3_percent: float,
    ) -> tuple[float, float, float]:
        raw_quantities = [
            executed_volume * (tp1_percent / 100.0),
            executed_volume * (tp2_percent / 100.0),
            executed_volume * (tp3_percent / 100.0),
        ]
        normalized_quantities = [
            mt5_client.normalize_order_volume(symbol, qty) if qty > 0 else 0.0
            for qty in raw_quantities
        ]
        total_normalized = sum(normalized_quantities)

        if total_normalized <= executed_volume + 1e-9:
            # If tp3_percent == 0, we should fill the remainder into TP2 instead
            # (i.e. two-TP configuration). Otherwise fill the last slot (TP3).
            if tp3_percent <= 0:
                fill_idx = 1
            else:
                fill_idx = 2
            normalized_quantities[fill_idx] = max(
                0.0,
                executed_volume - sum(normalized_quantities[:fill_idx]),
            )
            return (
                normalized_quantities[0],
                normalized_quantities[1],
                normalized_quantities[2],
            )

        # If partial quantities cannot be represented with the symbol lot step,
        # fallback to closing the full executed volume at the first TP.
        return (executed_volume, 0.0, 0.0)

    def print_watchlist(self, symbol_matches) -> None:
        """Print resolved watchlist and per-symbol entry-buffer info."""
        print("Resolved watchlist:")
        for match in symbol_matches:
            actual = match.actual or "NOT FOUND"
            # Determine canonical and per-symbol entry buffer if configured
            canonical = resolve_symbol_alias(actual or match.requested) or (actual or match.requested)
            cfg = self.config.symbol_scan_configs.get(canonical)
            if cfg is not None and getattr(cfg, "entry_buffer_min", None) is not None and getattr(cfg, "entry_buffer_min_unit", None) is not None:
                min_val = getattr(cfg, "entry_buffer_min")
                min_unit = getattr(cfg, "entry_buffer_min_unit")
                max_val = getattr(cfg, "entry_buffer_max")
                max_unit = getattr(cfg, "entry_buffer_max_unit")
                buffer_desc = f"{min_val}{min_unit}-{max_val}{max_unit}"
            else:
                buffer_desc = f"global {self.config.entry_pip_buffer} pips"
            print(f"  - {match.requested} -> {actual} (entry buffer: {buffer_desc})")

    def _monitor_positions(self, mt5_client: MT5Client) -> None:
        """Check open positions for TP-based partial closes."""
        if not self.open_positions:
            return

        tickets_to_remove = []
        state_changed = False
        
        for ticket, tracker in list(self.open_positions.items()):
            try:
                # Check if position still exists in MT5
                positions = mt5_client.get_open_positions(tracker.symbol)
                position_exists = any(p.ticket == ticket for p in positions)
                
                if not position_exists:
                    print(f"Position {ticket} no longer exists in MT5, removing tracker")
                    tickets_to_remove.append(ticket)
                    continue
                
                tick = mt5_client.get_tick(tracker.symbol)
                current_price = float(tick.bid) if tracker.direction == "bullish" else float(tick.ask)

                info: Any
                try:
                    info = mt5_client.get_symbol_market_status(tracker.symbol)
                    raw_point = info.get("point", 0.0001) if isinstance(info, dict) else getattr(info, "point", 0.0001)
                except Exception:
                    raw_point = 0.0001
                point = self._as_optional_float(raw_point) or 0.0001
                buffer_distance = (self.config.reversal_buffer_pips or 0.0) * float(point)

                if not hasattr(tracker, "tp1_reversal_armed"):
                    tracker.tp1_reversal_armed = False
                if not hasattr(tracker, "tp2_reversal_armed"):
                    tracker.tp2_reversal_armed = False

                tracker.highest_bid = (
                    float(tick.bid)
                    if getattr(tracker, "highest_bid", None) is None
                    else max(getattr(tracker, "highest_bid"), float(tick.bid))
                )
                tracker.lowest_ask = (
                    float(tick.ask)
                    if getattr(tracker, "lowest_ask", None) is None
                    else min(getattr(tracker, "lowest_ask"), float(tick.ask))
                )

                opposite_direction = "bearish" if tracker.direction == "bullish" else "bullish"
                if self._has_pending_execution_for_symbol_direction(tracker.symbol, opposite_direction):
                    if tracker.tp1_reversal_armed and not tracker.tp3_closed:
                        closing_event_ids = {
                            event_id
                            for event_id, (pending_event, _, _) in self.pending_execution_events.items()
                            if pending_event.symbol_actual == tracker.symbol
                            and pending_event.zone.direction == opposite_direction
                        }
                        pending_to_close = closing_event_ids - getattr(tracker, "opposite_pending_closed", set())
                        if pending_to_close:
                            close_condition = False
                            if tracker.direction == "bullish":
                                close_condition = current_price <= (tracker.take_profit_1 + buffer_distance)
                            else:
                                close_condition = current_price >= (tracker.take_profit_1 - buffer_distance)

                            if close_condition:
                                remaining = tracker.get_remaining_volume()
                                if remaining > 0:
                                    close_ticket = mt5_client.close_position_partial(
                                        tracker.symbol,
                                        tracker.direction,
                                        remaining,
                                        position_ticket=ticket,
                                        deviation=self.config.order_deviation,
                                        magic=self.config.order_magic_number,
                                    )
                                    if close_ticket:
                                        print(
                                            f"Opposite-side pending detected; closed remaining {remaining:.3f} of position {ticket} "
                                            f"at {current_price:.5f} because price reversed after TP1 and the opposite side is being hunted."
                                        )
                                        tracker.opposite_pending_closed.update(pending_to_close)
                                        tickets_to_remove.append(ticket)
                                        state_changed = True
                                        continue

                # Check for TP hits and close partial lots at the executable side.
                tp1_hit_this_cycle = False
                tp2_hit_this_cycle = False
                if not tracker.tp1_closed and self._check_tp_hit(tracker, current_price, 1):
                    tp1_hit_this_cycle = True
                    if tracker.tp1_qty > 0:
                        close_ticket = mt5_client.close_position_partial(
                            tracker.symbol,
                            tracker.direction,
                            tracker.tp1_qty,
                            position_ticket=ticket,
                            deviation=self.config.order_deviation,
                            magic=self.config.order_magic_number,
                        )
                        if close_ticket:
                            print(
                                f"Closed TP1 partial: {tracker.tp1_qty:.3f} of position {ticket} "
                                f"at {current_price:.5f} (target: {tracker.take_profit_1:.5f})"
                            )
                            # Send alert for TP1 hit
                            try:
                                tp1_alert = (
                                    f"📊 TP1 HIT - PARTIAL CLOSE\n"
                                    f"Symbol: {tracker.symbol}\n"
                                    f"Direction: {tracker.direction.upper()}\n"
                                    f"Ticket: {ticket}\n"
                                    f"Volume Closed: {tracker.tp1_qty:.3f}\n"
                                    f"Close Price: {current_price:.5f}\n"
                                    f"TP1 Target: {tracker.take_profit_1:.5f}"
                                )
                                self.notifier.send_text_alert(tp1_alert)
                            except Exception as exc:
                                print(f"Failed to send TP1 alert: {exc}")
                            tracker.tp1_closed = True
                            state_changed = True
                            # --- BREAKEVEN / TRAIL: move SL after TP1 hit ---
                            if not getattr(tracker, "breakeven_set", False):
                                canonical = tracker.symbol.upper().rstrip("mM").rstrip(".")
                                is_nasdaq = any(a in canonical for a in ["NASDAQ", "NAS100", "USTEC", "US100", "NAS"])

                                # Small pause to let the partial close settle and avoid race conditions
                                time.sleep(0.25)

                                # Verify the position still exists for this ticket
                                positions = mt5_client.get_open_positions(tracker.symbol)
                                pos_for_ticket = None
                                for p in positions:
                                    p_ticket = int(getattr(p, "ticket", getattr(p, "position", 0) or 0) or 0)
                                    if p_ticket == ticket:
                                        pos_for_ticket = p
                                        break
                                if not pos_for_ticket:
                                    print(f"Warning: position {ticket} not present after TP1 close; cannot set breakeven")
                                else:
                                    # Try a couple times to set SL (modify_sl already retries internally)
                                    if is_nasdaq:
                                        trail_sl = (
                                            tracker.take_profit_1 - 30.0
                                            if tracker.direction == "bullish"
                                            else tracker.take_profit_1 + 30.0
                                        )
                                        for attempt in range(2):
                                            be_ok = mt5_client.modify_sl(ticket, trail_sl)
                                            if be_ok:
                                                print(
                                                    f"NASDAQ trail SL set for {ticket}: SL moved to {trail_sl:.2f} (30pts behind TP1)"
                                                )
                                                tracker.breakeven_set = True
                                                break
                                            time.sleep(0.5)
                                        if not tracker.breakeven_set:
                                            print(f"Warning: failed to set NASDAQ trail SL for {ticket}")
                                    else:
                                        for attempt in range(2):
                                            be_ok = mt5_client.modify_sl(ticket, tracker.entry_price)
                                            if be_ok:
                                                print(
                                                    f"Breakeven set for {ticket}: SL moved to entry {tracker.entry_price:.5f}"
                                                )
                                                tracker.breakeven_set = True
                                                break
                                            time.sleep(0.5)
                                        if not tracker.breakeven_set:
                                            print(f"Warning: failed to set breakeven SL for {ticket}")
                    else:
                        tracker.tp1_closed = True
                        state_changed = True
                        if not getattr(tracker, "breakeven_set", False):
                            be_ok = mt5_client.modify_sl(ticket, tracker.entry_price)
                            if be_ok:
                                print(f"Breakeven set for {ticket}: SL moved to entry {tracker.entry_price:.5f}")
                                tracker.breakeven_set = True
                
                if not tracker.tp2_closed and self._check_tp_hit(tracker, current_price, 2):
                    tp2_hit_this_cycle = True
                    if tracker.tp2_qty > 0:
                        close_ticket = mt5_client.close_position_partial(
                            tracker.symbol,
                            tracker.direction,
                            tracker.tp2_qty,
                            position_ticket=ticket,
                            deviation=self.config.order_deviation,
                            magic=self.config.order_magic_number,
                        )
                        if close_ticket:
                            print(
                                f"Closed TP2 partial: {tracker.tp2_qty:.3f} of position {ticket} "
                                f"at {current_price:.5f} (target: {tracker.take_profit_2:.5f})"
                            )
                            # Send alert for TP2 hit
                            try:
                                tp2_alert = (
                                    f"📊 TP2 HIT - PARTIAL CLOSE\n"
                                    f"Symbol: {tracker.symbol}\n"
                                    f"Direction: {tracker.direction.upper()}\n"
                                    f"Ticket: {ticket}\n"
                                    f"Volume Closed: {tracker.tp2_qty:.3f}\n"
                                    f"Close Price: {current_price:.5f}\n"
                                    f"TP2 Target: {tracker.take_profit_2:.5f}"
                                )
                                self.notifier.send_text_alert(tp2_alert)
                            except Exception as exc:
                                print(f"Failed to send TP2 alert: {exc}")
                            tracker.tp2_closed = True
                            state_changed = True
                    else:
                        tracker.tp2_closed = True
                        state_changed = True

                # If TP2 was not explicitly closed but price has reached TP2, treat TP2 as reached
                if tracker.direction == "bullish":
                    tp2_reached = tracker.tp2_closed or (
                        getattr(tracker, "highest_bid", None) is not None
                        and getattr(tracker, "highest_bid") >= tracker.take_profit_2
                    )
                else:
                    tp2_reached = tracker.tp2_closed or (
                        getattr(tracker, "lowest_ask", None) is not None
                        and getattr(tracker, "lowest_ask") <= tracker.take_profit_2
                    )

                # If configured, apply a small reversal buffer (in pips/points) before exiting
                try:
                    info: Any = mt5_client.get_symbol_market_status(tracker.symbol)
                    if isinstance(info, dict):
                        raw_point = info.get("point", 0.0001)
                    elif hasattr(info, "get"):
                        # a Mock may implement get()
                        try:
                            raw_point = info.get("point", 0.0001)
                        except Exception:
                            raw_point = getattr(info, "point", 0.0001)
                    else:
                        raw_point = getattr(info, "point", 0.0001)
                    point = self._as_optional_float(raw_point) or 0.0001
                except Exception:
                    point = 0.0001
                buffer_distance = (self.config.reversal_buffer_pips or 0.0) * float(point)

                if tp2_reached:
                    tracker.tp2_reversal_armed = True
                if tracker.tp1_closed or (
                    tracker.direction == "bullish"
                    and getattr(tracker, "highest_bid", None) is not None
                    and getattr(tracker, "highest_bid") >= tracker.take_profit_1
                ) or (
                    tracker.direction == "bearish"
                    and getattr(tracker, "lowest_ask", None) is not None
                    and getattr(tracker, "lowest_ask") <= tracker.take_profit_1
                ):
                    tracker.tp1_reversal_armed = True

                # If price has reached TP2 and then changes direction, protect around TP2.
                if (
                    tracker.tp2_reversal_armed
                    and not tp2_hit_this_cycle
                    and not tracker.tp3_closed
                    and self.config.reversal_exit_enabled
                ):
                    try:
                        remaining = tracker.get_remaining_volume()
                        if remaining > 0:
                            if tracker.direction == "bullish":
                                print(
                                    f"Watching for bullish reversal after TP2: current_price={current_price:.5f}, "
                                    f"tp2={tracker.take_profit_2:.5f}, remaining={remaining:.3f}, buffer={buffer_distance:.6f}"
                                )
                                if current_price <= (tracker.take_profit_2 + buffer_distance):
                                    close_ticket = mt5_client.close_position_partial(
                                        tracker.symbol,
                                        tracker.direction,
                                        remaining,
                                        position_ticket=ticket,
                                        deviation=self.config.order_deviation,
                                        magic=self.config.order_magic_number,
                                    )
                                    if close_ticket:
                                        print(
                                            f"Reversal after TP2: closed remaining {remaining:.3f} of position {ticket} at {current_price:.5f}"
                                        )
                                        tickets_to_remove.append(ticket)
                                        state_changed = True
                            else:
                                print(
                                    f"Watching for bearish reversal after TP2: current_price={current_price:.5f}, "
                                    f"tp2={tracker.take_profit_2:.5f}, remaining={remaining:.3f}, buffer={buffer_distance:.6f}"
                                )
                                if current_price >= (tracker.take_profit_2 - buffer_distance):
                                    close_ticket = mt5_client.close_position_partial(
                                        tracker.symbol,
                                        tracker.direction,
                                        remaining,
                                        position_ticket=ticket,
                                        deviation=self.config.order_deviation,
                                        magic=self.config.order_magic_number,
                                    )
                                    if close_ticket:
                                        print(
                                            f"Reversal after TP2: closed remaining {remaining:.3f} of position {ticket} at {current_price:.5f}"
                                        )
                                        tickets_to_remove.append(ticket)
                                        state_changed = True
                    except Exception as exc:
                        print(f"Error handling reversal after TP2 for {ticket}: {exc}")

                # If price has reached TP1 but not TP2, protect around TP1.
                if tracker.direction == "bullish":
                    tp1_reached = tracker.tp1_closed or (
                        getattr(tracker, "highest_bid", None) is not None
                        and getattr(tracker, "highest_bid") >= tracker.take_profit_1
                    )
                else:
                    tp1_reached = tracker.tp1_closed or (
                        getattr(tracker, "lowest_ask", None) is not None
                        and getattr(tracker, "lowest_ask") <= tracker.take_profit_1
                    )
                if (
                    tp1_reached
                    and tracker.tp1_reversal_armed
                    and not tp1_hit_this_cycle
                    and not tracker.tp2_closed
                    and self.config.reversal_exit_enabled
                ):
                    try:
                        remaining = tracker.get_remaining_volume()
                        if remaining > 0:
                            if tracker.direction == "bullish":
                                print(
                                    f"Watching for bullish reversal after TP1: current_price={current_price:.5f}, "
                                    f"tp1={tracker.take_profit_1:.5f}, remaining={remaining:.3f}, buffer={buffer_distance:.6f}"
                                )
                                if current_price <= (tracker.take_profit_1 + buffer_distance):
                                    close_ticket = mt5_client.close_position_partial(
                                        tracker.symbol,
                                        tracker.direction,
                                        remaining,
                                        position_ticket=ticket,
                                        deviation=self.config.order_deviation,
                                        magic=self.config.order_magic_number,
                                    )
                                    if close_ticket:
                                        print(
                                            f"Reversal after TP1: closed remaining {remaining:.3f} of position {ticket} at {current_price:.5f}"
                                        )
                                        tickets_to_remove.append(ticket)
                                        state_changed = True
                            else:
                                print(
                                    f"Watching for bearish reversal after TP1: current_price={current_price:.5f}, "
                                    f"tp1={tracker.take_profit_1:.5f}, remaining={remaining:.3f}, buffer={buffer_distance:.6f}"
                                )
                                if current_price >= (tracker.take_profit_1 - buffer_distance):
                                    close_ticket = mt5_client.close_position_partial(
                                        tracker.symbol,
                                        tracker.direction,
                                        remaining,
                                        position_ticket=ticket,
                                        deviation=self.config.order_deviation,
                                        magic=self.config.order_magic_number,
                                    )
                                    if close_ticket:
                                        print(
                                            f"Reversal after TP1: closed remaining {remaining:.3f} of position {ticket} at {current_price:.5f}"
                                        )
                                        tickets_to_remove.append(ticket)
                                        state_changed = True
                    except Exception as exc:
                        print(f"Error handling reversal after TP1 for {ticket}: {exc}")

                if self._should_close_near_stop_loss(tracker, current_price, mt5_client, point):
                    try:
                        remaining = tracker.get_remaining_volume()
                        if remaining > 0:
                            close_ticket = mt5_client.close_position_partial(
                                tracker.symbol,
                                tracker.direction,
                                remaining,
                                position_ticket=ticket,
                                deviation=self.config.order_deviation,
                                magic=self.config.order_magic_number,
                            )
                            if close_ticket:
                                print(
                                    f"Closed remaining {remaining:.3f} of position {ticket} "
                                    f"near stop-loss {tracker.stop_loss:.5f} because the trade structure was invalidated."
                                )
                                try:
                                    close_alert = (
                                        f"⚠️ CLOSE NEAR SL - STOP LOSS PROXIMITY EXIT\n"
                                        f"Symbol: {tracker.symbol}\n"
                                        f"Direction: {tracker.direction.upper()}\n"
                                        f"Ticket: {ticket}\n"
                                        f"Volume Closed: {remaining:.3f}\n"
                                        f"Close Price: {current_price:.5f}\n"
                                        f"SL: {tracker.stop_loss:.5f}"
                                    )
                                    self.notifier.send_text_alert(close_alert)
                                except Exception as exc:
                                    print(f"Failed to send near-SL close alert: {exc}")
                                tickets_to_remove.append(ticket)
                                state_changed = True
                                continue
                    except Exception as exc:
                        print(f"Error handling near-stop-loss exit for {ticket}: {exc}")

                if not tracker.tp3_closed and self._check_tp_hit(tracker, current_price, 3):
                    remaining = tracker.get_remaining_volume()
                    if remaining > 0:
                        close_ticket = mt5_client.close_position_partial(
                            tracker.symbol,
                            tracker.direction,
                            remaining,
                            position_ticket=ticket,
                            deviation=self.config.order_deviation,
                            magic=self.config.order_magic_number,
                        )
                        if close_ticket:
                            print(
                                f"Closed final partial (TP3): {remaining:.3f} of position {ticket} "
                                f"at {current_price:.5f} (target: {tracker.take_profit_3:.5f})"
                            )
                            tracker.tp3_closed = True
                            tickets_to_remove.append(ticket)
                            state_changed = True
            except Exception as exc:
                print(f"Error monitoring position {ticket}: {exc}")
        
        # Remove fully closed or non-existent positions
        for ticket in set(tickets_to_remove):
            if self.open_positions.pop(ticket, None) is not None:
                state_changed = True

        if state_changed:
            self.save_position_trackers()

    @staticmethod
    def _check_tp_hit(tracker: PositionTracker, current_price: float, tp_level: int) -> bool:
        """Check if the current price has hit the specified TP level."""
        if tracker.direction == "bullish":
            # For long positions, price needs to reach or exceed TP
            if tp_level == 1:
                return current_price >= tracker.take_profit_1
            elif tp_level == 2:
                return current_price >= tracker.take_profit_2
            else:
                return current_price >= tracker.take_profit_3
        else:
            # For short positions, price needs to fall to or below TP
            if tp_level == 1:
                return current_price <= tracker.take_profit_1
            elif tp_level == 2:
                return current_price <= tracker.take_profit_2
            else:
                return current_price <= tracker.take_profit_3
