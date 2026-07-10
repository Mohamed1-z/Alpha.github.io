from __future__ import annotations

import pandas as pd

from swift_scanner.reporting import build_tp_sl_summary, evaluate_tp_sl_outcome


def make_future_bars(rows):
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


def test_evaluate_tp_sl_outcome_finds_bullish_take_profit_first():
    row = pd.Series(
        {
            "signal_label": "Main Long Entry",
            "signal_direction": "bullish",
            "price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "risk_reward_ratio": 2.0,
        }
    )
    future_bars = make_future_bars(
        [
            ("2026-06-01 00:15:00+00:00", 100.0, 105.0, 99.0, 104.0),
            ("2026-06-01 00:30:00+00:00", 104.0, 111.0, 103.0, 110.5),
        ]
    )

    outcome = evaluate_tp_sl_outcome(row, future_bars)

    assert outcome["tp_sl_analysis_complete"] is True
    assert outcome["tp_sl_outcome"] == "take_profit"
    assert outcome["tp_sl_bars_to_hit"] == 2
    assert outcome["tp_sl_result_r"] == 2.0


def test_evaluate_tp_sl_outcome_finds_bearish_stop_loss_first():
    row = pd.Series(
        {
            "signal_label": "Main Short Entry",
            "signal_direction": "bearish",
            "price": 100.0,
            "stop_loss": 105.0,
            "take_profit": 90.0,
            "risk_reward_ratio": 2.0,
        }
    )
    future_bars = make_future_bars(
        [("2026-06-01 00:15:00+00:00", 100.0, 106.0, 98.0, 104.0)]
    )

    outcome = evaluate_tp_sl_outcome(row, future_bars)

    assert outcome["tp_sl_analysis_complete"] is True
    assert outcome["tp_sl_outcome"] == "stop_loss"
    assert outcome["tp_sl_result_r"] == -1.0


def test_evaluate_tp_sl_outcome_marks_same_bar_ambiguity():
    row = pd.Series(
        {
            "signal_label": "Main Long Entry",
            "signal_direction": "bullish",
            "price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "risk_reward_ratio": 2.0,
        }
    )
    future_bars = make_future_bars(
        [("2026-06-01 00:15:00+00:00", 100.0, 111.0, 94.0, 101.0)]
    )

    outcome = evaluate_tp_sl_outcome(row, future_bars)

    assert outcome["tp_sl_analysis_complete"] is True
    assert outcome["tp_sl_outcome"] == "ambiguous_same_bar"
    assert outcome["tp_sl_notes"] == "TP and SL were both touched inside the same candle"


def test_build_tp_sl_summary_counts_outcomes():
    report = pd.DataFrame(
        [
            {
                "event_id": "1",
                "symbol_actual": "TEST",
                "timeframe": "M15",
                "expected_direction": "up",
                "risk_reward_ratio": 2.0,
                "tp_sl_analysis_complete": True,
                "tp_sl_outcome": "take_profit",
                "tp_sl_result_r": 2.0,
                "max_favorable_r": 2.2,
                "max_adverse_r": 0.2,
            },
            {
                "event_id": "2",
                "symbol_actual": "TEST",
                "timeframe": "M15",
                "expected_direction": "up",
                "risk_reward_ratio": 2.0,
                "tp_sl_analysis_complete": True,
                "tp_sl_outcome": "stop_loss",
                "tp_sl_result_r": -1.0,
                "max_favorable_r": 0.4,
                "max_adverse_r": 1.1,
            },
        ]
    )

    summary = build_tp_sl_summary(report)

    assert summary.iloc[0]["tp_hits"] == 1
    assert summary.iloc[0]["sl_hits"] == 1
    assert summary.iloc[0]["decided_alerts"] == 2
    assert summary.iloc[0]["tp_win_rate"] == 0.5
