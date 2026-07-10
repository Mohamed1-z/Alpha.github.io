from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from swift_scanner.config import ScannerConfig, TIMEFRAME_MAP, TimeframeConfig
from swift_scanner.mt5_client import MT5Client


def load_alert_history(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Alert history file not found: {path}")

    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    if not records:
        raise ValueError(f"Alert history file is empty: {path}")

    history = pd.DataFrame(records)
    history["triggered_at"] = pd.to_datetime(history["triggered_at"], utc=True)
    return history


def infer_expected_direction(row: pd.Series) -> str:
    label = str(row.get("signal_label", "") or "").lower()
    signal_direction = str(
        row.get("signal_direction", "")
        or row.get("order_block_direction", "")
        or row.get("zone_direction", "")
        or row.get("direction", "")
    ).lower()

    if signal_direction == "bullish" or "bullish" in label or "long" in label or "buy" in label:
        return "up"
    if signal_direction == "bearish" or "bearish" in label or "short" in label or "sell" in label:
        return "down"
    return "unknown"


def candle_direction(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "up"
    if close_price < open_price:
        return "down"
    return "neutral"


def infer_signal_direction(row: pd.Series) -> str:
    label = str(row.get("signal_label", "") or "").lower()
    direction = str(
        row.get("signal_direction", "")
        or row.get("order_block_direction", "")
        or row.get("zone_direction", "")
        or row.get("direction", "")
    ).lower()

    if direction == "bullish" or "bullish" in label or "long" in label or "buy" in label:
        return "bullish"
    if direction == "bearish" or "bearish" in label or "short" in label or "sell" in label:
        return "bearish"
    return "unknown"


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_datetime(value: object) -> pd.Timestamp | None:
    if value is None:
        return None
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp


def resolve_trade_levels(
    row: pd.Series,
    bars: pd.DataFrame,
    config: ScannerConfig,
) -> dict[str, float] | None:
    stop_loss = _as_float(row.get("stop_loss"))
    take_profit = _as_float(row.get("take_profit")) or _as_float(row.get("take_profit_3"))
    risk_reward_ratio = _as_float(row.get("risk_reward_ratio"))
    if risk_reward_ratio is None:
        entry_price = _as_float(row.get("entry_price")) or _as_float(row.get("price"))
        direction = infer_signal_direction(row)
        if entry_price is not None and stop_loss is not None and take_profit is not None:
            if direction == "bullish" and entry_price > stop_loss:
                risk_reward_ratio = (take_profit - entry_price) / (entry_price - stop_loss)
            elif direction == "bearish" and stop_loss > entry_price:
                risk_reward_ratio = (entry_price - take_profit) / (stop_loss - entry_price)
    if stop_loss is not None and take_profit is not None:
        return {
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward_ratio": risk_reward_ratio if risk_reward_ratio is not None else 0.0,
        }
    return None


def evaluate_tp_sl_outcome(row: pd.Series, future_bars: pd.DataFrame) -> dict[str, object]:
    outcome = {
        "tp_sl_analysis_complete": False,
        "tp_sl_outcome": "not_analyzed",
        "tp_sl_hit_time": None,
        "tp_sl_bars_to_hit": None,
        "tp_sl_result_r": None,
        "max_favorable_r": None,
        "max_adverse_r": None,
        "tp_sl_notes": "",
    }

    direction = infer_signal_direction(row)
    entry_price = _as_float(row.get("price"))
    stop_loss = _as_float(row.get("stop_loss"))
    take_profit = _as_float(row.get("take_profit"))
    risk_reward_ratio = _as_float(row.get("risk_reward_ratio")) or 0.0

    if direction not in {"bullish", "bearish"}:
        outcome["tp_sl_outcome"] = "unknown_direction"
        outcome["tp_sl_notes"] = "Cannot infer bullish or bearish direction"
        return outcome
    if entry_price is None or stop_loss is None or take_profit is None:
        outcome["tp_sl_outcome"] = "missing_levels"
        outcome["tp_sl_notes"] = "Missing entry, stop-loss, or take-profit"
        return outcome

    if direction == "bullish":
        risk = entry_price - stop_loss
        valid_levels = stop_loss < entry_price < take_profit
    else:
        risk = stop_loss - entry_price
        valid_levels = take_profit < entry_price < stop_loss
    if risk <= 0 or not valid_levels:
        outcome["tp_sl_outcome"] = "invalid_levels"
        outcome["tp_sl_notes"] = "TP/SL levels are not valid for the signal direction"
        return outcome
    if future_bars.empty:
        outcome["tp_sl_outcome"] = "no_future_bars"
        outcome["tp_sl_notes"] = "No future bars available for TP/SL analysis"
        return outcome

    max_favorable_r = 0.0
    max_adverse_r = 0.0
    outcome["tp_sl_analysis_complete"] = True
    outcome["tp_sl_outcome"] = "not_hit"

    for bars_to_hit, (_, bar) in enumerate(future_bars.iterrows(), start=1):
        high = float(bar["high"])
        low = float(bar["low"])
        hit_time = bar["time"].isoformat()

        if direction == "bullish":
            max_favorable_r = max(max_favorable_r, (high - entry_price) / risk)
            max_adverse_r = max(max_adverse_r, (entry_price - low) / risk)
            hit_tp = high >= take_profit
            hit_sl = low <= stop_loss
        else:
            max_favorable_r = max(max_favorable_r, (entry_price - low) / risk)
            max_adverse_r = max(max_adverse_r, (high - entry_price) / risk)
            hit_tp = low <= take_profit
            hit_sl = high >= stop_loss

        outcome["max_favorable_r"] = float(max_favorable_r)
        outcome["max_adverse_r"] = float(max_adverse_r)

        if hit_tp and hit_sl:
            outcome["tp_sl_outcome"] = "ambiguous_same_bar"
            outcome["tp_sl_hit_time"] = hit_time
            outcome["tp_sl_bars_to_hit"] = bars_to_hit
            outcome["tp_sl_notes"] = "TP and SL were both touched inside the same candle"
            return outcome
        if hit_tp:
            outcome["tp_sl_outcome"] = "take_profit"
            outcome["tp_sl_hit_time"] = hit_time
            outcome["tp_sl_bars_to_hit"] = bars_to_hit
            outcome["tp_sl_result_r"] = risk_reward_ratio
            return outcome
        if hit_sl:
            outcome["tp_sl_outcome"] = "stop_loss"
            outcome["tp_sl_hit_time"] = hit_time
            outcome["tp_sl_bars_to_hit"] = bars_to_hit
            outcome["tp_sl_result_r"] = -1.0
            return outcome

    outcome["tp_sl_notes"] = "Neither TP nor SL was touched in the lookahead window"
    outcome["max_favorable_r"] = float(max_favorable_r)
    outcome["max_adverse_r"] = float(max_adverse_r)
    return outcome


def _count_bool(values: pd.Series) -> int:
    return int(values.fillna(False).astype(bool).sum())


def _mean_number(values: pd.Series) -> float:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    return float(clean.mean()) if not clean.empty else 0.0


def build_tp_sl_summary(report: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        report.groupby(
            ["symbol_actual", "timeframe", "expected_direction", "risk_reward_ratio"],
            dropna=False,
        )
        .agg(
            total_alerts=("event_id", "count"),
            analyzed_alerts=("tp_sl_analysis_complete", _count_bool),
            tp_hits=("tp_sl_outcome", lambda values: int((values == "take_profit").sum())),
            sl_hits=("tp_sl_outcome", lambda values: int((values == "stop_loss").sum())),
            same_bar_hits=(
                "tp_sl_outcome",
                lambda values: int((values == "ambiguous_same_bar").sum()),
            ),
            not_hit=("tp_sl_outcome", lambda values: int((values == "not_hit").sum())),
            mean_result_r=("tp_sl_result_r", _mean_number),
            mean_max_favorable_r=("max_favorable_r", _mean_number),
            mean_max_adverse_r=("max_adverse_r", _mean_number),
        )
        .reset_index()
    )
    grouped["decided_alerts"] = grouped["tp_hits"] + grouped["sl_hits"]
    grouped["tp_win_rate"] = grouped.apply(
        lambda row: (
            float(row["tp_hits"] / row["decided_alerts"])
            if int(row["decided_alerts"]) > 0
            else 0.0
        ),
        axis=1,
    )
    return grouped


def _prepare_for_excel(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in output.columns:
        if pd.api.types.is_datetime64_any_dtype(output[column]):
            output[column] = output[column].astype(str)
    return output


def analyze_alert_history(
    config: ScannerConfig,
    history_path: Path,
    report_path: Path,
    lookahead_bars: int = 40,
) -> Path:
    history = load_alert_history(history_path)
    analysis_rows: list[dict[str, object]] = []

    with MT5Client(config) as client:
        for _, row in history.iterrows():
            expected_direction = infer_expected_direction(row)
            analysis_row = row.to_dict()
            analysis_row["expected_direction"] = expected_direction
            analysis_row["analysis_complete"] = False
            analysis_row["opposite_candles_before_direction"] = None
            analysis_row["same_direction_candles_before_direction"] = None
            analysis_row["first_expected_bar_time"] = None
            analysis_row["first_opposite_bar_time"] = None
            analysis_row["analysis_notes"] = ""
            analysis_row["stop_loss"] = _as_float(row.get("stop_loss"))
            analysis_row["take_profit"] = _as_float(row.get("take_profit"))
            analysis_row["risk_reward_ratio"] = _as_float(row.get("risk_reward_ratio"))
            analysis_row.update(evaluate_tp_sl_outcome(pd.Series(analysis_row), pd.DataFrame()))

            timeframe_name = str(row["timeframe"])
            if timeframe_name not in TIMEFRAME_MAP:
                analysis_row["analysis_notes"] = f"Unsupported timeframe: {timeframe_name}"
                analysis_row["tp_sl_notes"] = f"Unsupported timeframe: {timeframe_name}"
                analysis_rows.append(analysis_row)
                continue

            timeframe = TimeframeConfig(name=timeframe_name, mt5_code=TIMEFRAME_MAP[timeframe_name])
            try:
                bars = client.fetch_rates(str(row["symbol_actual"]), timeframe)
            except Exception as exc:
                analysis_row["analysis_notes"] = f"Data fetch failed: {exc}"
                analysis_row["tp_sl_notes"] = f"Data fetch failed: {exc}"
                analysis_rows.append(analysis_row)
                continue

            triggered_at = pd.to_datetime(row["triggered_at"], utc=True)
            future_bars = bars[bars["time"] > triggered_at].head(lookahead_bars)
            if future_bars.empty:
                analysis_row["analysis_notes"] = "No future bars available for analysis"
                analysis_row["tp_sl_notes"] = "No future bars available for TP/SL analysis"
                analysis_rows.append(analysis_row)
                continue

            trade_levels = resolve_trade_levels(row, bars, config)
            if trade_levels:
                analysis_row.update(trade_levels)
            analysis_row.update(
                evaluate_tp_sl_outcome(pd.Series(analysis_row), future_bars)
            )

            future_bars = future_bars.assign(
                direction=future_bars.apply(
                    lambda bar: candle_direction(bar["open"], bar["close"]), axis=1
                )
            )

            if expected_direction not in ("up", "down"):
                analysis_row["analysis_notes"] = "Expected direction unknown"
            else:
                opposite_direction = "down" if expected_direction == "up" else "up"
                directions = future_bars["direction"].tolist()
                first_expected_index = next(
                    (idx for idx, d in enumerate(directions) if d == expected_direction), None
                )
                first_opposite_index = next(
                    (idx for idx, d in enumerate(directions) if d == opposite_direction), None
                )

                if first_expected_index is None and first_opposite_index is None:
                    analysis_row["analysis_notes"] = "No meaningful direction in lookahead window"
                else:
                    same_before = 0
                    opposite_before = 0
                    if first_expected_index is not None:
                        opposite_before = sum(
                            1
                            for d in directions[:first_expected_index]
                            if d == opposite_direction
                        )
                        same_before = sum(
                            1
                            for d in directions[:first_expected_index]
                            if d == expected_direction
                        )
                    elif first_opposite_index is not None:
                        opposite_before = sum(
                            1
                            for d in directions[: first_opposite_index + 1]
                            if d == opposite_direction
                        )
                    analysis_row["analysis_complete"] = True
                    analysis_row["opposite_candles_before_direction"] = opposite_before
                    analysis_row["same_direction_candles_before_direction"] = same_before
                    if first_expected_index is not None:
                        analysis_row["first_expected_bar_time"] = future_bars.iloc[first_expected_index]["time"].isoformat()
                    if first_opposite_index is not None:
                        analysis_row["first_opposite_bar_time"] = future_bars.iloc[first_opposite_index]["time"].isoformat()
                    if opposite_before > 0:
                        analysis_row["analysis_notes"] = (
                            "Price showed opposite candles before expected direction"
                        )
                    else:
                        analysis_row["analysis_notes"] = "Expected direction occurred without opposite candles first"

            analysis_rows.append(analysis_row)

    report = pd.DataFrame(analysis_rows)
    if "triggered_at" in report.columns:
        report["triggered_at"] = pd.to_datetime(report["triggered_at"], utc=True)
    tp_sl_summary = build_tp_sl_summary(report)

    summary = (
        report.groupby(["symbol_actual", "timeframe", "expected_direction"])
        .agg(
            total_alerts=("event_id", "count"),
            false_before_direction=("opposite_candles_before_direction", lambda values: int(sum(v for v in values if isinstance(v, (int, float)) and v > 0))),
            mean_opposite_candles=("opposite_candles_before_direction", lambda values: float(pd.Series(values).dropna().mean()) if len(pd.Series(values).dropna()) > 0 else 0.0),
            analyzed_alerts=("analysis_complete", lambda values: int(sum(bool(v) for v in values))),
        )
        .reset_index()
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(report_path, engine="openpyxl") as writer:
        _prepare_for_excel(report).to_excel(writer, sheet_name="Alerts", index=False)
        _prepare_for_excel(summary).to_excel(writer, sheet_name="Summary", index=False)
        _prepare_for_excel(tp_sl_summary).to_excel(
            writer, sheet_name="TP_SL_Summary", index=False
        )

    return report_path
