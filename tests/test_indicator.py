from __future__ import annotations

import pandas as pd
import pytest

from swift_scanner.config import ScannerConfig
from swift_scanner.indicator import (
    _entry_lines,
    _line_from_entry,
    _make_event,
    SwiftLines,
    scan_swift,
)


def make_frame(rows):
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


def swift_test_config(**overrides) -> ScannerConfig:
    defaults = {
        "main_use_alternate_signals": False,
        "main_basis_length": 1,
        "main_basis_type": "ALMA",
        "use_trend_filter": False,
    }
    defaults.update(overrides)
    return ScannerConfig(**defaults)


def test_scans_swift_main_long_entry():
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.1, 9.7, 9.8, 1000),
            ("2026-05-25 00:15:00+00:00", 10.0, 10.4, 9.9, 10.2, 1000),
        ]
    )
    config = swift_test_config()

    result = scan_swift(data, "TEST", "TEST", "M15", config)

    assert len(result.fresh_events) == 1
    event = result.fresh_events[0]
    assert event.signal_source == "swift"
    assert event.signal_label == "Main Long Entry"
    assert event.zone.direction == "bullish"
    assert event.entry_price == 10.2
    assert event.stop_loss == pytest.approx(10.2 * 0.995)
    assert event.take_profit_1 == pytest.approx(10.2 * 1.01)
    assert event.take_profit_2 == pytest.approx(10.2 * 1.015)
    assert event.take_profit_3 == pytest.approx(10.2 * 1.02)
    assert event.risk_reward_ratio == pytest.approx(4.0)

    reloaded = type(event).from_dict(event.to_dict())
    assert reloaded.signal_source == "swift"
    assert reloaded.take_profit_1 == event.take_profit_1
    assert reloaded.take_profit_2 == event.take_profit_2
    assert reloaded.take_profit_3 == event.take_profit_3
    assert event.lesson.startswith("Lesson:")
    assert reloaded.lesson == event.lesson


def test_displacement_filter_blocks_weak_trigger_candle():
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.1, 9.7, 9.8, 1000),
            # Crosses up, but closes only 60% through the range, below the 70% displacement threshold.
            ("2026-05-25 00:15:00+00:00", 10.0, 10.4, 9.9, 10.2, 1000),
        ]
    )
    config = swift_test_config()

    result = scan_swift(data, "TEST", "TEST", "M15", config)

    assert len(result.fresh_events) == 1
    assert result.fresh_events[0].signal_label == "Main Long Entry"


def test_displacement_filter_allows_strong_trigger_candle():
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.0, 9.8, 9.8, 1000),
            ("2026-05-25 00:15:00+00:00", 9.9, 10.3, 9.8, 10.25, 1000),
        ]
    )
    config = swift_test_config()

    result = scan_swift(data, "TEST", "TEST", "M15", config)

    assert len(result.fresh_events) == 1
    assert result.fresh_events[0].signal_label == "Main Long Entry"


def test_entry_lines_put_bearish_take_profits_below_entry():
    lines = _entry_lines(
        entry=0.70655,
        direction="bearish",
        tp1_percent=1.0,
        tp2_percent=1.5,
        tp3_percent=2.0,
        sl_percent=0.5,
    )

    assert lines.stop_loss == pytest.approx(0.70655 * 1.005)
    assert lines.take_profit_1 == pytest.approx(0.70655 * 0.99)
    assert lines.take_profit_2 == pytest.approx(0.70655 * 0.985)
    assert lines.take_profit_3 == pytest.approx(0.70655 * 0.98)
    assert lines.take_profit_3 < lines.take_profit_2 < lines.take_profit_1 < lines.entry < lines.stop_loss


def test_line_from_entry_preserves_active_position_levels():
    previous_value = 79.7
    source = 79.5
    # if a position is already active (previous_condition != 0), a new opposite trigger should not overwrite
    assert _line_from_entry(
        trigger_long=False,
        trigger_short=True,
        previous_condition=1.0,
        previous_value=previous_value,
        source=source,
        long_percent=1.0,
        short_percent=1.5,
        target_condition=1.0,
    ) == previous_value
    assert _line_from_entry(
        trigger_long=True,
        trigger_short=False,
        previous_condition=0.0,
        previous_value=previous_value,
        source=source,
        long_percent=1.0,
        short_percent=1.5,
        target_condition=1.0,
    ) == pytest.approx(source + source * 0.01)


def test_line_from_entry_preserves_active_position_when_triggered_again():
    previous_value = 79.7
    source = 79.5
    # if there is already an active position, a new trigger should not overwrite existing levels
    assert _line_from_entry(
        trigger_long=True,
        trigger_short=False,
        previous_condition=1.0,
        previous_value=previous_value,
        source=source,
        long_percent=1.0,
        short_percent=1.5,
        target_condition=1.0,
    ) == previous_value
    assert _line_from_entry(
        trigger_long=False,
        trigger_short=True,
        previous_condition=-1.0,
        previous_value=previous_value,
        source=source,
        long_percent=1.0,
        short_percent=1.5,
        target_condition=-1.0,
    ) == previous_value


def test_make_event_assigns_correct_take_profit_for_tp_levels():
    rows = [
        ("2026-05-25 00:00:00+00:00", 10.0, 10.1, 9.7, 9.8, 1000)
    ]
    data = make_frame(rows)
    lines = SwiftLines(
        entry=10.0,
        stop_loss=9.95,
        take_profit_1=10.10,
        take_profit_2=10.15,
        take_profit_3=10.20,
    )

    event_tp1 = _make_event(
        data,
        "TEST",
        "TEST",
        "M15",
        0,
        0,
        "bullish",
        "Main Long TP1",
        10.0,
        lines,
    )
    assert event_tp1.take_profit == pytest.approx(10.10)

    event_tp2 = _make_event(
        data,
        "TEST",
        "TEST",
        "M15",
        0,
        0,
        "bullish",
        "Main Long TP2",
        10.0,
        lines,
    )
    assert event_tp2.take_profit == pytest.approx(10.15)

    event_tp3 = _make_event(
        data,
        "TEST",
        "TEST",
        "M15",
        0,
        0,
        "bullish",
        "Main Long TP3",
        10.0,
        lines,
    )
    assert event_tp3.take_profit == pytest.approx(10.20)


def test_scan_does_not_realert_old_swift_signal():
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.1, 9.7, 9.8, 1000),
            ("2026-05-25 00:15:00+00:00", 10.0, 10.4, 9.9, 10.2, 1000),
            ("2026-05-25 00:30:00+00:00", 10.2, 10.25, 10.0, 10.15, 1000),
            ("2026-05-25 00:45:00+00:00", 10.15, 10.2, 10.1, 10.18, 1000),
        ]
    )
    config = swift_test_config()

    result = scan_swift(data, "TEST", "TEST", "M15", config)

    assert result.zones
    assert result.fresh_events == []


def test_scan_accepts_recent_events_with_last_bar_lookback():
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.1, 9.7, 9.8, 1000),
            ("2026-05-25 00:15:00+00:00", 10.0, 10.4, 9.9, 10.2, 1000),
            ("2026-05-25 00:30:00+00:00", 10.0, 10.2, 9.95, 10.15, 1000),
        ]
    )
    config = swift_test_config()

    result = scan_swift(data, "TEST", "TEST", "M15", config)

    assert len(result.fresh_events) == 1
    assert result.fresh_events[0].zone.formed_index == 1


def test_scan_accepts_recent_events_for_alternate_signal_chunks():
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.3, 9.8, 9.9, 1000),
            ("2026-05-25 00:15:00+00:00", 10.0, 10.3, 9.8, 9.9, 1000),
            ("2026-05-25 00:30:00+00:00", 10.0, 10.3, 9.8, 9.9, 1000),
            ("2026-05-25 00:45:00+00:00", 10.0, 10.3, 9.8, 9.9, 1000),
            ("2026-05-25 01:00:00+00:00", 10.1, 10.5, 9.9, 10.4, 1000),
            ("2026-05-25 01:15:00+00:00", 10.1, 10.5, 9.9, 10.4, 1000),
            ("2026-05-25 01:30:00+00:00", 10.1, 10.5, 9.9, 10.4, 1000),
            ("2026-05-25 01:45:00+00:00", 10.1, 10.5, 9.9, 10.4, 1000),
        ]
    )
    config = swift_test_config(main_use_alternate_signals=True, main_alternate_multiplier=4)

    result = scan_swift(data, "TEST", "TEST", "M15", config)

    assert len(result.fresh_events) == 1
    assert result.fresh_events[0].zone.formed_index == 4
