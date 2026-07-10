import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from swift_scanner.notifier import SignalNotifier
from swift_scanner.config import ScannerConfig
from swift_scanner.models import SignalEvent, Zone
from datetime import datetime
import requests


def make_config(tmp_path: Path) -> ScannerConfig:
    cfg = ScannerConfig()
    cfg.telegram_bot_token = "fake-token"
    cfg.telegram_chat_ids = ["1234"]
    cfg.telegram_silent = False
    cfg.output_dir = tmp_path
    return cfg


def make_event() -> SignalEvent:
    zone = Zone(
        zone_type="swift_signal",
        direction="bullish",
        symbol_request="TEST",
        symbol_actual="TEST",
        created_at=datetime.utcnow(),
        left_index=0,
        right_index=1,
        top=1.0,
        bottom=0.5,
    )
    ev = SignalEvent(
        symbol_request="TEST",
        symbol_actual="TEST",
        timeframe="M5",
        triggered_at=datetime.utcnow(),
        price=1.2345,
        zone=zone,
    )
    ev.entry_price = 1.2345
    ev.stop_loss = 1.2300
    ev.take_profit_1 = 1.2400
    ev.take_profit_2 = 1.2450
    ev.take_profit_3 = 1.2500
    return ev


@patch("swift_scanner.notifier.requests.Session.post")
def test_telegram_image_upload_ssleof_fallback(mock_post, tmp_path):
    # Simulate SSLError on image upload, and verify text-only fallback is attempted
    from requests.exceptions import SSLError

    # First, post (image) raises SSLError
    mock_post.side_effect = SSLError("EOF occurred in violation of protocol")

    cfg = make_config(tmp_path)
    notifier = SignalNotifier(cfg)

    ev = make_event()
    # create a fake image file
    img = tmp_path / "img.png"
    img.write_bytes(b"PNGDATA")

    # send_telegram should attempt image upload, fail, then try text-only fallback
    # text-only uses _post_with_retries -> post as well, so side_effect applies there too,
    # but our notifier will call send_text_alert() which also uses _post_with_retries.
    # To simulate that the code tries send_text_alert and that succeeds, we patch
    # the notifier.send_text_alert to record calls and return.
    with patch.object(notifier, "send_text_alert", return_value=1) as mock_text:
        notifier.send_telegram(ev, img)
        mock_text.assert_called()


@patch("swift_scanner.notifier.requests.Session.post")
def test_telegram_total_failure_logged(mock_post, tmp_path):
    # Simulate failures for both image and text upload and ensure fallback is logged
    from requests.exceptions import SSLError

    mock_post.side_effect = SSLError("EOF occurred in violation of protocol")

    cfg = make_config(tmp_path)
    notifier = SignalNotifier(cfg)

    ev = make_event()
    img = tmp_path / "img.png"
    img.write_bytes(b"PNGDATA")

    # Ensure send_text_alert also fails by letting mock_post raise for both calls
    with pytest.raises(RuntimeError):
        notifier.send_telegram(ev, img)

    # fallback file should exist
    fallback = cfg.output_dir / "telegram_fallback.jsonl"
    assert fallback.exists()
    content = fallback.read_text()
    assert "telegram_failure_reason" in content
