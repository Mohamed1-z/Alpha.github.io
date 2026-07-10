from __future__ import annotations

# If this module is executed directly, ensure the package root is on sys.path
# so package-style imports (e.g. `from swift_scanner.config import ...`) work.
if __name__ == "__main__":
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from swift_scanner.config import ScannerConfig
from swift_scanner.models import ScanResult, SignalEvent, Zone


SwiftPayload = dict[str, Any]

TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H2": 120,
    "H4": 240,
    "D1": 1440,
}


@dataclass(slots=True)
class SwiftLines:
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float


def _to_datetime(value: object) -> datetime:
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return pd.Timestamp(value).to_pydatetime()


def _as_bool(value: object) -> bool:
    return bool(value) and not pd.isna(value)


def _is_number(value: object) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _nz(value: object, fallback: float = 0.0) -> float:
    return float(value) if _is_number(value) else fallback


def _ema(series: pd.Series, length: int) -> pd.Series:
    length = max(1, int(length))
    return series.astype(float).ewm(span=length, adjust=False, min_periods=1).mean()


def _sma(series: pd.Series, length: int) -> pd.Series:
    length = max(1, int(length))
    return series.astype(float).rolling(length, min_periods=length).mean()


def _wma(series: pd.Series, length: int) -> pd.Series:
    length = max(1, int(round(length)))
    weights = np.arange(1, length + 1, dtype=float)

    def weighted(values: np.ndarray) -> float:
        return float(np.dot(values, weights) / weights.sum())

    return series.astype(float).rolling(length, min_periods=length).apply(weighted, raw=True)


def _vwma(series: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    length = max(1, int(length))
    price_volume = series.astype(float) * volume.astype(float)
    volume_sum = volume.astype(float).rolling(length, min_periods=length).sum()
    return price_volume.rolling(length, min_periods=length).sum() / volume_sum


def _smma(series: pd.Series, length: int) -> pd.Series:
    length = max(1, int(length))
    values = series.astype(float).to_numpy()
    output = np.full(len(values), np.nan, dtype=float)
    for idx, value in enumerate(values):
        if np.isnan(value):
            continue
        if idx == 0 or np.isnan(output[idx - 1]):
            window = values[max(0, idx - length + 1) : idx + 1]
            clean = window[~np.isnan(window)]
            output[idx] = float(clean.mean()) if len(clean) else np.nan
        else:
            output[idx] = ((output[idx - 1] * (length - 1)) + value) / length
    return pd.Series(output, index=series.index)


def _linreg(series: pd.Series, length: int, offset: int) -> pd.Series:
    length = max(1, int(length))
    offset = int(offset)
    x = np.arange(length, dtype=float)
    x_mean = float(x.mean())
    denominator = float(((x - x_mean) ** 2).sum())

    def regress(values: np.ndarray) -> float:
        if np.isnan(values).any():
            return np.nan
        y_mean = float(values.mean())
        slope = float(((x - x_mean) * (values - y_mean)).sum() / denominator) if denominator else 0.0
        intercept = y_mean - slope * x_mean
        return intercept + slope * (length - 1 - offset)

    return series.astype(float).rolling(length, min_periods=length).apply(regress, raw=True)


def _alma(series: pd.Series, length: int, offset: float, sigma: float) -> pd.Series:
    length = max(1, int(length))
    sigma = float(sigma) if float(sigma) > 0 else 1.0
    m = offset * (length - 1)
    s = length / sigma
    positions = np.arange(length, dtype=float)
    weights = np.exp(-((positions - m) ** 2) / (2 * s * s))
    weights = weights / weights.sum()

    def alma_value(values: np.ndarray) -> float:
        if np.isnan(values).any():
            return np.nan
        return float(np.dot(values, weights))

    return series.astype(float).rolling(length, min_periods=length).apply(alma_value, raw=True)


def _supersmoother(series: pd.Series, length: int) -> pd.Series:
    length = max(1, int(length))
    values = series.astype(float).to_numpy()
    output = np.full(len(values), np.nan, dtype=float)
    a1 = np.exp(-1.414 * np.pi / length)
    b1 = 2 * a1 * np.cos(1.414 * np.pi / length)
    c2 = b1
    c3 = -(a1 * a1)
    c1 = 1 - c2 - c3

    for idx, value in enumerate(values):
        if np.isnan(value):
            continue
        prev_src = _nz(values[idx - 1] if idx > 0 else np.nan, value)
        prev_1 = _nz(output[idx - 1] if idx > 0 else np.nan)
        prev_2 = _nz(output[idx - 2] if idx > 1 else np.nan)
        output[idx] = c1 * (value + prev_src) / 2 + c2 * prev_1 + c3 * prev_2
    return pd.Series(output, index=series.index)


def variant(
    basis_type: str,
    source: pd.Series,
    length: int,
    offset_sigma: int,
    offset_alma: float,
    volume: pd.Series | None = None,
) -> pd.Series:
    """Python conversion of the Pine `variant(...)` moving-average selector."""
    length = max(1, int(length))
    source = source.astype(float)
    basis = basis_type.upper()

    sma = _sma(source, length)
    ema = _ema(source, length)
    ema_of_ema = _ema(ema, length)
    dema = 2 * ema - ema_of_ema
    tema = 3 * (ema - ema_of_ema) + _ema(ema_of_ema, length)
    wma = _wma(source, length)
    vwma = _vwma(source, volume, length) if volume is not None else wma
    smma = _smma(source, length)
    half_length = max(1, int(round(length / 2)))
    hull_length = max(1, int(round(np.sqrt(length))))
    hull = _wma(2 * _wma(source, half_length) - _wma(source, length), hull_length)
    lsma = _linreg(source, length, offset_sigma)
    alma = _alma(source, length, offset_alma, offset_sigma)
    tma = _sma(sma, length)
    ssma = _supersmoother(source, length)

    if basis == "EMA":
        return ema
    if basis == "DEMA":
        return dema
    if basis == "TEMA":
        return tema
    if basis == "WMA":
        return wma
    if basis == "VWMA":
        return vwma
    if basis == "SMMA":
        return smma
    if basis == "HULLMA":
        return hull
    if basis == "LSMA":
        return lsma
    if basis == "ALMA":
        return alma
    if basis == "TMA":
        return tma
    if basis == "SSMA":
        return ssma
    return tma


def _heikin_ashi(frame: pd.DataFrame) -> pd.DataFrame:
    ha_close = (frame["open"] + frame["high"] + frame["low"] + frame["close"]) / 4
    ha_open = np.full(len(frame), np.nan, dtype=float)
    for idx, row in enumerate(frame.itertuples(index=False)):
        if idx == 0:
            ha_open[idx] = (float(row.open) + float(row.close)) / 2
        else:
            ha_open[idx] = (ha_open[idx - 1] + float(ha_close.iloc[idx - 1])) / 2

    ha_high = pd.concat(
        [frame["high"].astype(float), pd.Series(ha_open, index=frame.index), ha_close],
        axis=1,
    ).max(axis=1)
    ha_low = pd.concat(
        [frame["low"].astype(float), pd.Series(ha_open, index=frame.index), ha_close],
        axis=1,
    ).min(axis=1)
    return pd.DataFrame(
        {
            "time": frame["time"].values,
            "open": ha_open,
            "high": ha_high.to_numpy(),
            "low": ha_low.to_numpy(),
            "close": ha_close.to_numpy(),
            "volume": frame["volume"].to_numpy(dtype=float),
        },
        index=frame.index,
    )


def _resample_ohlc(frame: pd.DataFrame, timeframe_name: str, multiplier: int) -> pd.DataFrame:
    multiplier = max(1, int(multiplier))
    minutes = TIMEFRAME_MINUTES.get(timeframe_name.upper())
    if minutes is None or multiplier <= 1:
        return frame.copy()

    working = frame.copy()
    working["time"] = pd.to_datetime(working["time"], utc=True)
    indexed = working.set_index("time")
    rule = f"{minutes * multiplier}min"
    resampled = indexed.resample(rule, label="left", closed="left", origin="epoch").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    resampled = resampled.dropna(subset=["open", "high", "low", "close"])
    resampled = resampled.reset_index()
    return resampled


def _alternate_series(
    base_frame: pd.DataFrame,
    timeframe_name: str,
    use_alternate: bool,
    multiplier: int,
    basis_type: str,
    basis_length: int,
    offset_sigma: int,
    offset_alma: float,
    delay_offset: int,
    heikin_ashi: bool,
) -> tuple[pd.Series, pd.Series]:
    signal_frame = (
        _resample_ohlc(base_frame, timeframe_name, multiplier)
        if use_alternate
        else base_frame.copy()
    )
    signal_frame = signal_frame.reset_index(drop=True)
    if heikin_ashi:
        signal_frame = _heikin_ashi(signal_frame)

    close_source = signal_frame["close"].astype(float).shift(delay_offset)
    open_source = signal_frame["open"].astype(float).shift(delay_offset)
    volume = signal_frame["volume"].astype(float)
    close_ma = variant(
        basis_type,
        close_source,
        basis_length,
        offset_sigma,
        offset_alma,
        volume,
    )
    open_ma = variant(
        basis_type,
        open_source,
        basis_length,
        offset_sigma,
        offset_alma,
        volume,
    )

    if not use_alternate:
        return (
            pd.Series(close_ma.to_numpy(), index=base_frame.index),
            pd.Series(open_ma.to_numpy(), index=base_frame.index),
        )

    signal_times = pd.to_datetime(signal_frame["time"], utc=True)
    base_times = pd.to_datetime(base_frame["time"], utc=True)
    close_by_time = pd.Series(close_ma.to_numpy(), index=signal_times)
    open_by_time = pd.Series(open_ma.to_numpy(), index=signal_times)
    return (
        close_by_time.reindex(base_times, method="ffill").reset_index(drop=True),
        open_by_time.reindex(base_times, method="ffill").reset_index(drop=True),
    )


def _crossover(first: pd.Series, second: pd.Series) -> pd.Series:
    return (first > second) & (first.shift(1) <= second.shift(1))


def _crossunder(first: pd.Series, second: pd.Series) -> pd.Series:
    return (first < second) & (first.shift(1) >= second.shift(1))


def _is_displacement_candle(
    idx: int,
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr: pd.Series,
    direction: str,
    body_ratio_min: float = 0.55,
    atr_multiplier_min: float = 1.0,
    close_position_min: float = 0.70,
) -> bool:
    """Return True if the candle at idx shows genuine displacement in the signal direction.

    Displacement is defined as:
    1. Candle body >= body_ratio_min of total candle range (committed, not wick-dominated)
    2. Candle range >= atr_multiplier_min * ATR (above-average momentum, not a tiny candle)
    3. Close is in the top 30% of range for bullish, bottom 30% for bearish (strong close)
    4. Candle direction matches signal direction (bullish cross = bullish candle body)
    """
    if idx < 1:
        return False
    try:
        o = float(open_.iloc[idx])
        h = float(high.iloc[idx])
        l = float(low.iloc[idx])
        c = float(close.iloc[idx])
        atr_val = float(atr.iloc[idx])
        total_range = h - l
        if total_range <= 0 or atr_val <= 0:
            return False

        body = abs(c - o)
        body_ratio = body / total_range

        # Check 1: body must dominate the candle
        if body_ratio < body_ratio_min:
            return False

        # Check 2: candle must be above-average size
        if total_range < atr_multiplier_min * atr_val:
            return False

        # Check 3: close position within range
        close_position = (c - l) / total_range  # 0 = closed at low, 1 = at high
        if direction == "bullish":
            if c <= o:  # candle must be bullish
                return False
            if close_position < close_position_min:
                return False
        else:  # bearish
            if c >= o:  # candle must be bearish
                return False
            if close_position > (1.0 - close_position_min):
                return False

        return True
    except Exception:
        return False


def _cross_value(values: pd.Series, lines: np.ndarray, index: int, over: bool) -> bool:
    if index <= 0:
        return False
    current_value = values.iloc[index]
    previous_value = values.iloc[index - 1]
    current_line = lines[index]
    previous_line = lines[index - 1]
    if not all(_is_number(item) for item in [current_value, previous_value, current_line, previous_line]):
        return False
    if over:
        return float(current_value) > float(current_line) and float(previous_value) <= float(previous_line)
    return float(current_value) < float(current_line) and float(previous_value) >= float(previous_line)


def _smooth_range(source: pd.Series, period: int, multiplier: float) -> pd.Series:
    period = max(1, int(period))
    weighted_period = period * 2 - 1
    average_range = _ema((source.astype(float) - source.astype(float).shift(1)).abs(), period)
    return _ema(average_range, weighted_period) * float(multiplier)


def _range_filter(source: pd.Series, ranges: pd.Series) -> pd.Series:
    source_values = source.astype(float).to_numpy()
    range_values = ranges.astype(float).to_numpy()
    output = np.full(len(source_values), np.nan, dtype=float)
    for idx, value in enumerate(source_values):
        if np.isnan(value):
            continue
        previous = _nz(output[idx - 1] if idx > 0 else np.nan, value)
        current_range = _nz(range_values[idx])
        if value > previous:
            output[idx] = previous if value - current_range < previous else value - current_range
        else:
            output[idx] = previous if value + current_range > previous else value + current_range
    return pd.Series(output, index=source.index)


def _trend_filter(
    close: pd.Series,
    period: int,
    multiplier: float,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    smooth = _smooth_range(close, period, multiplier)
    filtered = _range_filter(close, smooth)
    values = filtered.to_numpy(dtype=float)
    direction = np.zeros(len(values), dtype=float)
    for idx in range(1, len(values)):
        if values[idx] > values[idx - 1]:
            direction[idx] = 1.0
        elif values[idx] < values[idx - 1]:
            direction[idx] = -1.0
        else:
            direction[idx] = direction[idx - 1]
    up = pd.Series(direction, index=close.index)
    buy_signal = (up == 1) & (up.shift(1) == -1)
    sell_signal = (up == -1) & (up.shift(1) == 1)
    return up, buy_signal, sell_signal


def _line_from_entry(
    trigger_long: bool,
    trigger_short: bool,
    previous_condition: float,
    previous_value: float,
    source: float,
    long_percent: float,
    short_percent: float,
    target_condition: float,
) -> float:
    top_level = source + (source * (long_percent / 100))
    bottom_level = source - (source * (short_percent / 100))
    if previous_condition != 0.0:
        return previous_value
    if trigger_long:
        return float(top_level)
    if trigger_short:
        return float(bottom_level)
    return previous_value


def _entry_lines(
    entry: float,
    direction: str,
    tp1_percent: float,
    tp2_percent: float,
    tp3_percent: float,
    sl_percent: float,
) -> SwiftLines:
    if direction == "bullish":
        return SwiftLines(
            entry=entry,
            stop_loss=entry - (entry * (sl_percent / 100)),
            take_profit_1=entry + (entry * (tp1_percent / 100)),
            take_profit_2=entry + (entry * (tp2_percent / 100)),
            take_profit_3=entry + (entry * (tp3_percent / 100)),
        )
    return SwiftLines(
        entry=entry,
        stop_loss=entry + (entry * (sl_percent / 100)),
        take_profit_1=entry - (entry * (tp1_percent / 100)),
        take_profit_2=entry - (entry * (tp2_percent / 100)),
        take_profit_3=entry - (entry * (tp3_percent / 100)),
    )


def _entry_lines_from_deltas(
    entry: float,
    direction: str,
    tp1_delta: float,
    tp2_delta: float,
    tp3_delta: float,
    sl_delta: float,
) -> SwiftLines:
    if direction == "bullish":
        return SwiftLines(
            entry=entry,
            stop_loss=entry - sl_delta,
            take_profit_1=entry + tp1_delta,
            take_profit_2=entry + tp2_delta,
            take_profit_3=entry + tp3_delta,
        )
    return SwiftLines(
        entry=entry,
        stop_loss=entry + sl_delta,
        take_profit_1=entry - tp1_delta,
        take_profit_2=entry - tp2_delta,
        take_profit_3=entry - tp3_delta,
    )


def _configured_entry_lines(entry: float, direction: str, config: ScannerConfig, prefix: str) -> SwiftLines:
    tp1_delta = getattr(config, f"{prefix}_tp1_delta")
    tp2_delta = getattr(config, f"{prefix}_tp2_delta")
    tp3_delta = getattr(config, f"{prefix}_tp3_delta")
    sl_delta = getattr(config, f"{prefix}_sl_delta")
    if None not in (tp1_delta, tp2_delta, tp3_delta, sl_delta):
        return _entry_lines_from_deltas(
            entry,
            direction,
            float(tp1_delta),
            float(tp2_delta),
            float(tp3_delta),
            float(sl_delta),
        )
    return _entry_lines(
        entry,
        direction,
        float(getattr(config, f"{prefix}_tp1_percent")),
        float(getattr(config, f"{prefix}_tp2_percent")),
        float(getattr(config, f"{prefix}_tp3_percent")),
        float(getattr(config, f"{prefix}_sl_percent")),
    )


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    period = max(1, int(period))
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.rolling(period, min_periods=1).mean()


def _entry_lines_dynamic(
    entry: float,
    direction: str,
    recent_high: float,
    recent_low: float,
    atr_value: float,
    sl_multiplier: float,
    tp_multiplier: float,
) -> SwiftLines:
    atr_value = float(atr_value) if _is_number(atr_value) else 0.0
    atr_value = max(atr_value, abs(entry) * 0.00001)

    if direction == "bullish":
        stop_loss = recent_low - atr_value * sl_multiplier
        take_profit_1 = entry + atr_value * tp_multiplier
        take_profit_2 = entry + atr_value * tp_multiplier * 1.5
        take_profit_3 = entry + atr_value * tp_multiplier * 2.0
    else:
        stop_loss = recent_high + atr_value * sl_multiplier
        take_profit_1 = entry - atr_value * tp_multiplier
        take_profit_2 = entry - atr_value * tp_multiplier * 1.5
        take_profit_3 = entry - atr_value * tp_multiplier * 2.0

    return SwiftLines(
        entry=entry,
        stop_loss=float(stop_loss),
        take_profit_1=float(take_profit_1),
        take_profit_2=float(take_profit_2),
        take_profit_3=float(take_profit_3),
    )


def _risk_reward(lines: SwiftLines, direction: str) -> float | None:
    if direction == "bullish":
        risk = lines.entry - lines.stop_loss
        reward = lines.take_profit_3 - lines.entry
    else:
        risk = lines.stop_loss - lines.entry
        reward = lines.entry - lines.take_profit_3
    if risk <= 0:
        return None
    return float(reward / risk)


def _make_zone(
    data: pd.DataFrame,
    symbol_request: str,
    symbol_actual: str,
    index: int,
    entry_index: int,
    direction: str,
    lines: SwiftLines,
    formed_index: int | None = None,
) -> Zone:
    prices = [
        lines.entry,
        lines.stop_loss,
        lines.take_profit_1,
        lines.take_profit_2,
        lines.take_profit_3,
    ]
    return Zone(
        zone_type="swift_signal",
        direction=direction,
        symbol_request=symbol_request,
        symbol_actual=symbol_actual,
        created_at=_to_datetime(data["time"].iloc[entry_index]),
        left_index=entry_index,
        right_index=index,
        top=float(max(prices)),
        bottom=float(min(prices)),
          formed_index=entry_index if formed_index is None else formed_index,
    )


def _make_event(
    data: pd.DataFrame,
    symbol_request: str,
    symbol_actual: str,
    timeframe_name: str,
    index: int,
    entry_index: int,
    direction: str,
    label: str,
    price: float,
    lines: SwiftLines,
) -> SignalEvent:
    zone = _make_zone(
        data,
        symbol_request,
        symbol_actual,
        index,
        entry_index,
        direction,
        lines,
    )
    label_lower = label.lower()
    if "tp1" in label_lower:
        take_profit = float(lines.take_profit_1)
    elif "tp2" in label_lower:
        take_profit = float(lines.take_profit_2)
    else:
        take_profit = float(lines.take_profit_3)

    normalized_timeframe = timeframe_name.strip().upper()
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
    if normalized_timeframe in aliases:
        normalized_timeframe = aliases[normalized_timeframe]

    return SignalEvent(
        symbol_request=symbol_request,
        symbol_actual=symbol_actual,
        timeframe=normalized_timeframe,
        triggered_at=_to_datetime(data["time"].iloc[index]),
        price=float(price),
        zone=zone,
        signal_source="swift",
        signal_label=label,
        explanation=_build_signal_explanation(label, direction),
        entry_price=float(lines.entry),
        stop_loss=float(lines.stop_loss),
        take_profit=take_profit,
        take_profit_1=float(lines.take_profit_1),
        take_profit_2=float(lines.take_profit_2),
        take_profit_3=float(lines.take_profit_3),
        risk_reward_ratio=_risk_reward(lines, direction),
        lesson=_build_signal_lesson(label, direction),
    )


def _build_signal_explanation(label: str, direction: str) -> str:
    label_lower = label.lower()
    if "entry" in label_lower:
        if "long" in label_lower:
            return (
                "Bullish entry: price crossed into a SWIFT bullish setup with trend filter support. "
                "Entry, SL, and TP levels are placed to manage risk and reward."
            )
        if "short" in label_lower:
            return (
                "Bearish entry: price crossed into a SWIFT bearish setup with trend filter support. "
                "Entry, SL, and TP levels are placed to manage risk and reward."
            )
        return (
            "SWIFT entry signal: price moved into a valid zone and triggered the main trade setup. "
            "Risk levels are mapped for proper trade management."
        )
    if "tp1" in label_lower or "tp2" in label_lower or "tp3" in label_lower:
        return (
            f"Profit target reached ({label}). This is an exit phase of the original {direction} SWIFT trade. "
            "Review how price moved after the entry and how the take-profit level was hit."
        )
    if "sl" in label_lower:
        return (
            f"Stop loss hit ({label}). This closes the prior {direction} SWIFT trade to protect capital. "
            "Review the setup to understand why the trade did not continue."
        )
    return (
        f"SWIFT signal: {label}. This reflects the current price action within the trade zone. "
        "Compare the zone, candle action, and risk levels to learn the setup."
    )


def _build_signal_lesson(label: str, direction: str) -> str:
    label_lower = label.lower()
    if "entry" in label_lower:
        return "Lesson: entries should align with trend and use defined risk zones, not guesswork."
    if "tp" in label_lower:
        return "Lesson: take profit signals show where momentum completed the move and reinforced the setup."
    if "sl" in label_lower:
        return "Lesson: stop loss signals remind you that risk control is the most important part of trading."
    if "long" in label_lower or "bearish" in label_lower:
        return "Lesson: watch how price behaved inside the SWIFT zone before the signal was confirmed."
    return "Lesson: review the signal zone and risk levels to learn the system over time."


def _trade_allowed(trade_type: str, direction: str) -> bool:
    trade_type = trade_type.upper()
    if trade_type == "NONE":
        return False
    if trade_type == "BOTH":
        return True
    if direction == "bullish":
        return trade_type == "LONG"
    return trade_type == "SHORT"


def _bar_in_window(timestamp: object, config: ScannerConfig) -> bool:
    current = pd.Timestamp(timestamp)
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    start = pd.Timestamp(
        year=config.backtest_start_year,
        month=config.backtest_start_month,
        day=config.backtest_start_day,
        hour=0,
        minute=0,
        tz="UTC",
    )
    end = pd.Timestamp(
        year=config.backtest_end_year,
        month=config.backtest_end_month,
        day=config.backtest_end_day,
        hour=23,
        minute=59,
        tz="UTC",
    )
    return bool(start <= current <= end)


def _copy_lines(entry: float, stop: float, tp1: float, tp2: float, tp3: float) -> SwiftLines:
    return SwiftLines(
        entry=float(entry),
        stop_loss=float(stop),
        take_profit_1=float(tp1),
        take_profit_2=float(tp2),
        take_profit_3=float(tp3),
    )


def detect_swift_signals(
    data: pd.DataFrame,
    symbol_request: str,
    symbol_actual: str,
    timeframe_name: str,
    config: ScannerConfig,
) -> tuple[pd.DataFrame, list[SignalEvent], list[Zone]]:
    if data.empty:
        return data.copy(), [], []

    working = data.copy().reset_index(drop=True)
    working["time"] = pd.to_datetime(working["time"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        working[column] = working[column].astype(float)

    close = working["close"]
    high = working["high"]
    low = working["low"]
    atr_series = _atr(high, low, close, config.atr_period)

    trend_direction, buy_signal, sell_signal = _trend_filter(
        close,
        config.trend_period,
        config.trend_multiplier,
    )

    main_close, main_open = _alternate_series(
        working,
        timeframe_name,
        config.main_use_alternate_signals,
        config.main_alternate_multiplier,
        config.main_basis_type,
        config.main_basis_length,
        config.main_offset_sigma,
        config.main_offset_alma,
        config.main_delay_offset,
        config.main_heikin_ashi,
    )

    main_cross_over = _crossover(main_close, main_open)
    main_cross_under = _crossunder(main_close, main_open)

    length = len(working)
    main_condition = np.zeros(length, dtype=float)
    main_entry_line = np.full(length, np.nan, dtype=float)
    main_stop_line = np.full(length, np.nan, dtype=float)
    main_tp1_line = np.full(length, np.nan, dtype=float)
    main_tp2_line = np.full(length, np.nan, dtype=float)
    main_tp3_line = np.full(length, np.nan, dtype=float)
    last_main_direction_series = np.zeros(length, dtype=int)

    last_main_direction = 0
    main_position_closed = True
    main_entry_index: int | None = None
    events: list[SignalEvent] = []

    for idx in range(length):
        previous_main_condition = main_condition[idx - 1] if idx > 0 else 0.0
        previous_main_entry = main_entry_line[idx - 1] if idx > 0 else np.nan
        previous_main_stop = main_stop_line[idx - 1] if idx > 0 else np.nan
        previous_main_tp1 = main_tp1_line[idx - 1] if idx > 0 else np.nan
        previous_main_tp2 = main_tp2_line[idx - 1] if idx > 0 else np.nan
        previous_main_tp3 = main_tp3_line[idx - 1] if idx > 0 else np.nan

        trend_allows_main_long = (
            not config.use_trend_filter
            or _as_bool(buy_signal.iloc[idx])
            or trend_direction.iloc[idx] == 1
        )
        trend_allows_main_short = (
            not config.use_trend_filter
            or _as_bool(sell_signal.iloc[idx])
            or trend_direction.iloc[idx] == -1
        )
        main_le_trigger = (
            config.main_enabled
            and _as_bool(main_cross_over.iloc[idx])
            and trend_allows_main_long
        )
        main_se_trigger = (
            config.main_enabled
            and _as_bool(main_cross_under.iloc[idx])
            and trend_allows_main_short
        )
        # Displacement gating has been removed: all valid signal crosses are accepted.
        main_displacement_long = True
        main_displacement_short = True

        main_new_long = (
            main_le_trigger
            and previous_main_condition <= 0.0
            and main_position_closed
            and main_displacement_long
        )
        main_new_short = (
            main_se_trigger
            and previous_main_condition >= 0.0
            and main_position_closed
            and main_displacement_short
        )

        price = float(close.iloc[idx])
        main_entry_line[idx] = (
            price
            if main_new_long
            else price
            if main_new_short
            else previous_main_entry
        )
        if config.enable_dynamic_levels and idx >= config.dynamic_level_lookback:
            if main_new_long:
                recent_high = float(
                    high.iloc[max(0, idx - config.dynamic_level_lookback) : idx].max()
                )
                recent_low = float(
                    low.iloc[max(0, idx - config.dynamic_level_lookback) : idx].min()
                )
                main_lines = _entry_lines_dynamic(
                    price,
                    "bullish",
                    recent_high,
                    recent_low,
                    float(atr_series.iloc[idx]),
                    config.dynamic_sl_atr_multiplier,
                    config.dynamic_tp_atr_multiplier,
                )
                main_stop_line[idx] = main_lines.stop_loss
                main_tp1_line[idx] = main_lines.take_profit_1
                main_tp2_line[idx] = main_lines.take_profit_2
                main_tp3_line[idx] = main_lines.take_profit_3
            elif main_new_short:
                recent_high = float(
                    high.iloc[max(0, idx - config.dynamic_level_lookback) : idx].max()
                )
                recent_low = float(
                    low.iloc[max(0, idx - config.dynamic_level_lookback) : idx].min()
                )
                main_lines = _entry_lines_dynamic(
                    price,
                    "bearish",
                    recent_high,
                    recent_low,
                    float(atr_series.iloc[idx]),
                    config.dynamic_sl_atr_multiplier,
                    config.dynamic_tp_atr_multiplier,
                )
                main_stop_line[idx] = main_lines.stop_loss
                main_tp1_line[idx] = main_lines.take_profit_1
                main_tp2_line[idx] = main_lines.take_profit_2
                main_tp3_line[idx] = main_lines.take_profit_3
            else:
                main_stop_line[idx] = previous_main_stop
                main_tp3_line[idx] = previous_main_tp3
                main_tp2_line[idx] = previous_main_tp2
                main_tp1_line[idx] = previous_main_tp1
        else:
            if main_new_long or main_new_short:
                main_lines = _configured_entry_lines(
                    price,
                    "bullish" if main_new_long else "bearish",
                    config,
                    "main",
                )
                main_stop_line[idx] = main_lines.stop_loss
                main_tp1_line[idx] = main_lines.take_profit_1
                main_tp2_line[idx] = main_lines.take_profit_2
                main_tp3_line[idx] = main_lines.take_profit_3
            else:
                main_stop_line[idx] = previous_main_stop
                main_tp3_line[idx] = previous_main_tp3
                main_tp2_line[idx] = previous_main_tp2
                main_tp1_line[idx] = previous_main_tp1

        main_sl_long = _cross_value(low, main_stop_line, idx, over=False)
        main_sl_short = _cross_value(high, main_stop_line, idx, over=True)
        main_tp3_long = _cross_value(high, main_tp3_line, idx, over=True)
        main_tp3_short = _cross_value(low, main_tp3_line, idx, over=False)
        main_tp2_long = _cross_value(high, main_tp2_line, idx, over=True)
        main_tp2_short = _cross_value(low, main_tp2_line, idx, over=False)
        main_tp1_long = _cross_value(high, main_tp1_line, idx, over=True)
        main_tp1_short = _cross_value(low, main_tp1_line, idx, over=False)

        new_main_condition = previous_main_condition
        if main_new_long:
            new_main_condition = 1.0
            last_main_direction = 1
            main_position_closed = False
            main_entry_index = idx
        elif main_new_short:
            new_main_condition = -1.0
            last_main_direction = -1
            main_position_closed = False
            main_entry_index = idx
        elif main_tp3_long and previous_main_condition == 1.2:
            new_main_condition = 1.3
            main_position_closed = True
        elif main_tp3_short and previous_main_condition == -1.2:
            new_main_condition = -1.3
            main_position_closed = True
        elif main_tp2_long and previous_main_condition == 1.1:
            new_main_condition = 1.2
        elif main_tp2_short and previous_main_condition == -1.1:
            new_main_condition = -1.2
        elif main_tp1_long and previous_main_condition == 1.0:
            new_main_condition = 1.1
            main_position_closed = True
        elif main_tp1_short and previous_main_condition == -1.0:
            new_main_condition = -1.1
            main_position_closed = True
        elif main_sl_long and previous_main_condition >= 1.0:
            new_main_condition = 0.0
            main_position_closed = True
        elif main_sl_short and previous_main_condition <= -1.0:
            new_main_condition = 0.0
            main_position_closed = True
        main_condition[idx] = new_main_condition

        last_main_direction_series[idx] = last_main_direction

        in_window = _bar_in_window(working["time"].iloc[idx], config)
        main_long_allowed = _trade_allowed(config.main_trade_type, "bullish")
        main_short_allowed = _trade_allowed(config.main_trade_type, "bearish")

        main_lines = _copy_lines(
            main_entry_line[idx],
            main_stop_line[idx],
            main_tp1_line[idx],
            main_tp2_line[idx],
            main_tp3_line[idx],
        )

        main_long_entry = (
            main_new_long
            and new_main_condition == 1.0
        )
        main_short_entry = (
            main_new_short
            and new_main_condition == -1.0
        )
        main_long_tp1 = (
            main_tp1_long and previous_main_condition == 1.0 and new_main_condition == 1.1
        )
        main_short_tp1 = (
            main_tp1_short and previous_main_condition == -1.0 and new_main_condition == -1.1
        )
        main_long_tp2 = (
            main_tp2_long and previous_main_condition == 1.1 and new_main_condition == 1.2
        )
        main_short_tp2 = (
            main_tp2_short and previous_main_condition == -1.1 and new_main_condition == -1.2
        )
        main_long_tp3 = (
            main_tp3_long and previous_main_condition == 1.2 and new_main_condition == 1.3
        )
        main_short_tp3 = (
            main_tp3_short and previous_main_condition == -1.2 and new_main_condition == -1.3
        )
        main_long_sl = (
            main_sl_long and previous_main_condition >= 1.0 and new_main_condition == 0.0
        )
        main_short_sl = (
            main_sl_short and previous_main_condition <= -1.0 and new_main_condition == 0.0
        )


        if in_window and main_long_allowed and main_long_entry:
            events.append(
                _make_event(
                    working,
                    symbol_request,
                    symbol_actual,
                    timeframe_name,
                    idx,
                    main_entry_index if main_entry_index is not None else idx,
                    "bullish",
                    "Main Long Entry",
                    price,
                    main_lines,
                )
            )
        if in_window and main_short_allowed and main_short_entry:
            events.append(
                _make_event(
                    working,
                    symbol_request,
                    symbol_actual,
                    timeframe_name,
                    idx,
                    main_entry_index if main_entry_index is not None else idx,
                    "bearish",
                    "Main Short Entry",
                    price,
                    main_lines,
                )
            )
        if main_entry_index is not None:
            if main_long_tp1:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bullish",
                        "Main Long TP1",
                        main_tp1_line[idx],
                        main_lines,
                    )
                )
            if main_short_tp1:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bearish",
                        "Main Short TP1",
                        main_tp1_line[idx],
                        main_lines,
                    )
                )
            if main_long_tp2:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bullish",
                        "Main Long TP2",
                        main_tp2_line[idx],
                        main_lines,
                    )
                )
            if main_short_tp2:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bearish",
                        "Main Short TP2",
                        main_tp2_line[idx],
                        main_lines,
                    )
                )
            if main_long_tp3:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bullish",
                        "Main Long TP3",
                        main_tp3_line[idx],
                        main_lines,
                    )
                )
            if main_short_tp3:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bearish",
                        "Main Short TP3",
                        main_tp3_line[idx],
                        main_lines,
                    )
                )
            if main_long_sl:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bullish",
                        "Main Long SL",
                        main_stop_line[idx],
                        main_lines,
                    )
                )
                main_entry_index = None
            if main_short_sl:
                events.append(
                    _make_event(
                        working,
                        symbol_request,
                        symbol_actual,
                        timeframe_name,
                        idx,
                        main_entry_index,
                        "bearish",
                        "Main Short SL",
                        main_stop_line[idx],
                        main_lines,
                    )
                )
                main_entry_index = None

    working["swift_main_condition"] = main_condition
    working["swift_main_entry"] = main_entry_line
    working["swift_main_stop"] = main_stop_line
    working["swift_main_tp1"] = main_tp1_line
    working["swift_main_tp2"] = main_tp2_line
    working["swift_main_tp3"] = main_tp3_line
    working["swift_last_main_direction"] = last_main_direction_series
    working["swift_trend_direction"] = trend_direction.to_numpy()

    active_zones: list[Zone] = []
    last_index = length - 1
    latest_main_condition = main_condition[last_index]
    if latest_main_condition in {1.0, 1.1, 1.2, -1.0, -1.1, -1.2}:
        direction = "bullish" if latest_main_condition > 0 else "bearish"
        if all(
            _is_number(value)
            for value in [
                main_entry_line[last_index],
                main_stop_line[last_index],
                main_tp1_line[last_index],
                main_tp2_line[last_index],
                main_tp3_line[last_index],
            ]
        ):
            lines = _copy_lines(
                main_entry_line[last_index],
                main_stop_line[last_index],
                main_tp1_line[last_index],
                main_tp2_line[last_index],
                main_tp3_line[last_index],
            )
            entry_candidates = [
                event.zone.left_index
                for event in reversed(events)
                if event.signal_label.startswith("Main ")
            ]
            active_zones.append(
                _make_zone(
                    working,
                    symbol_request,
                    symbol_actual,
                    last_index,
                    entry_candidates[0] if entry_candidates else last_index,
                    direction,
                    lines,
                       formed_index=entry_candidates[0] if entry_candidates else last_index,
                )
            )

    return working, events, active_zones


def scan_swift(
    data: pd.DataFrame,
    symbol_request: str,
    symbol_actual: str,
    timeframe_name: str,
    config: ScannerConfig,
) -> ScanResult:
    working = data.copy().reset_index(drop=True)
    latest_close = float(working.iloc[-1]["close"])
    last_bar_time = _to_datetime(working.iloc[-1]["time"])
    if len(working) < 2:
        return ScanResult(
            symbol_request=symbol_request,
            symbol_actual=symbol_actual,
            timeframe=timeframe_name,
            bars=working,
            zones=[],
            fresh_events=[],
            latest_close=latest_close,
            last_bar_time=last_bar_time,
        )

    bars, events, active_zones = detect_swift_signals(
        working,
        symbol_request,
        symbol_actual,
        timeframe_name,
        config,
    )
    last_index = len(bars) - 1
    if config.main_use_alternate_signals and config.main_alternate_multiplier > 1:
        fresh_threshold = max(0, last_index - config.main_alternate_multiplier + 1)
    else:
        fresh_threshold = max(0, last_index - 1)
    fresh_events = [
        event
        for event in events
        if event.zone.formed_index >= fresh_threshold
    ]
    zones_by_key = {zone.key: zone for zone in active_zones}
    for event in fresh_events:
        zones_by_key.setdefault(event.zone.key, event.zone)

    return ScanResult(
        symbol_request=symbol_request,
        symbol_actual=symbol_actual,
        timeframe=timeframe_name,
        bars=bars,
        zones=list(zones_by_key.values()),
        fresh_events=fresh_events,
        latest_close=float(bars.iloc[-1]["close"]),
        last_bar_time=_to_datetime(bars.iloc[-1]["time"]),
    )
