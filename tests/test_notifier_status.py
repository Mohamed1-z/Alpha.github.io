from swift_scanner.notifier import SignalNotifier
from swift_scanner.config import ScannerConfig
from pathlib import Path


def make_config(tmp_path: Path) -> ScannerConfig:
    cfg = ScannerConfig()
    cfg.telegram_bot_token = "fake-token"
    cfg.telegram_chat_ids = ["1234"]
    cfg.output_dir = tmp_path
    return cfg


def test_notifier_status_initial(tmp_path: Path):
    cfg = make_config(tmp_path)
    notifier = SignalNotifier(cfg)
    status = notifier.get_telegram_status()
    assert isinstance(status, dict)
    assert status["ssl_error_count"] == 0
    assert status["in_text_only_mode"] is False
    assert status["text_only_until"] == 0.0
