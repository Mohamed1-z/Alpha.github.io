from __future__ import annotations

from app import build_parser
from swift_scanner.config import DEFAULT_TIMEFRAME_NAMES, build_timeframes, parse_chat_ids


def test_parse_chat_ids_supports_multiple_sources():
    chat_ids = parse_chat_ids("12345, 67890", "67890", "24680;13579")

    assert chat_ids == ["12345", "67890", "24680", "13579"]


def test_default_timeframes_use_five_and_fifteen_minute_scans():
    assert DEFAULT_TIMEFRAME_NAMES == ["M5", "M15"]


def test_build_timeframes_supports_fifteen_minute():
    timeframes = build_timeframes(["M15"])

    assert [tf.name for tf in timeframes] == ["M15"]


def test_parser_defaults_to_two_cent_lots():
    args = build_parser().parse_args([])

    assert args.order_volume == 0.02


def test_parser_defaults_max_confirmation_attempts():
    args = build_parser().parse_args([])

    assert args.max_confirmation_attempts == 1


def test_parser_defaults_to_tp3_target_for_partial_close_ladder():
    args = build_parser().parse_args([])

    assert args.tp_target_level == "tp3"
