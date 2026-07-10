from __future__ import annotations

import json
import platform
from pathlib import Path
import time
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from swift_scanner.charting import format_signal_explanation
from swift_scanner.config import ScannerConfig
from swift_scanner.models import SignalEvent


def _current_tp_target(event: SignalEvent, config: ScannerConfig | None = None) -> tuple[str, float | None]:
    configured_target = str(getattr(config, "tp_target_level", "tp3") or "tp3").strip().lower()
    if configured_target in {"tp1", "tp2", "tp3"}:
        values = {
            "tp1": event.take_profit_1,
            "tp2": event.take_profit_2,
            "tp3": event.take_profit_3,
        }
        value = values.get(configured_target)
        if value is not None:
            return configured_target.upper(), value

    label_lower = (event.signal_label or "").lower()
    if "tp1" in label_lower:
        return "TP1", event.take_profit_1
    if "tp2" in label_lower:
        return "TP2", event.take_profit_2
    if "tp3" in label_lower:
        return "TP3", event.take_profit_3
    if "entry" in label_lower:
        return "TP3", event.take_profit_3 if event.take_profit_3 is not None else event.take_profit
    return "TP3", event.take_profit


def format_trade_levels(event: SignalEvent, config: ScannerConfig | None = None) -> str:
    if event.stop_loss is None:
        return ""
    rr = (
        f"{event.risk_reward_ratio:.2f}R"
        if event.risk_reward_ratio is not None
        else "n/a"
    )
    entry = f"entry={event.entry_price:.5f} | " if event.entry_price is not None else ""
    ticket = f"ticket={event.order_ticket} | " if event.order_ticket is not None else ""
    tp1 = f" | tp1={event.take_profit_1:.5f}" if event.take_profit_1 is not None else ""
    tp2 = f" | tp2={event.take_profit_2:.5f}" if event.take_profit_2 is not None else ""
    tp3 = f" | tp3={event.take_profit_3:.5f}" if event.take_profit_3 is not None else ""
    target_label, target_value = _current_tp_target(event, config)
    target = (
        f" | target={target_label}={target_value:.5f}"
        if target_value is not None
        else ""
    )
    return f" | {ticket}{entry}sl={event.stop_loss:.5f}{tp1}{tp2}{tp3}{target} | rr={rr}"


class SignalNotifier:
    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        # create a requests session with retries for transient network errors
        session = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=4,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self._session = session
        # Track SSL errors to auto-fallback to text-only mode
        self._ssl_error_count = 0
        self._text_only_until = 0.0
        # Ensure status file path
        try:
            self._status_file = self.config.output_dir / "telegram_status.json"
        except Exception:
            self._status_file = None

    def get_telegram_status(self) -> dict:
        """Return internal Telegram status useful for monitoring/debugging.

        Keys:
        - ssl_error_count: int
        - in_text_only_mode: bool
        - text_only_until: float (epoch seconds)
        """
        return {
            "ssl_error_count": int(getattr(self, "_ssl_error_count", 0)),
            "in_text_only_mode": time.time() < getattr(self, "_text_only_until", 0.0),
            "text_only_until": float(getattr(self, "_text_only_until", 0.0)),
        }

    def _post_with_retries(self, url: str, **kwargs) -> requests.Response:
        """Perform a POST with manual retries for TLS and transient network errors.

        This supplements urllib3's retry by catching SSLError and other
        RequestException cases that may not be retried automatically. Uses
        exponential backoff with jitter and prints brief retry logs.
        """
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._session.post(url, **kwargs)
                # success -> reset ssl error counter
                self._ssl_error_count = 0
                return response
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # Increment SSL error counter when appropriate
                if isinstance(exc, requests.exceptions.SSLError):
                    self._ssl_error_count += 1
                    threshold = int(getattr(self.config, "telegram_ssl_error_threshold", 3))
                    cooldown = int(getattr(self.config, "telegram_text_only_cooldown_seconds", 300))
                    if self._ssl_error_count >= threshold:
                        # enter text-only cooldown window and abort image retries
                        self._text_only_until = time.time() + cooldown
                        print(f"Telegram POST attempt {attempt}/{max_attempts} failed with SSLError: {exc}; entering text-only mode for {cooldown}s")
                        # persist status for external monitoring
                        try:
                            if getattr(self, "_status_file", None):
                                import json as _json

                                self._status_file.parent.mkdir(parents=True, exist_ok=True)
                                self._status_file.write_text(
                                    _json.dumps(self.get_telegram_status()),
                                    encoding="utf-8",
                                )
                        except Exception:
                            pass
                        raise
                if attempt == max_attempts:
                    # re-raise so callers can log fallback
                    raise
                base = 0.5 * (2 ** (attempt - 1))
                delay = base + random.uniform(0, base)
                print(f"Telegram POST attempt {attempt}/{max_attempts} failed: {exc}; retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            except requests.exceptions.RequestException:
                # For other RequestException types, bubble up to let caller handle
                raise

    def notify(self, event: SignalEvent, image_path: Path) -> None:
        trade_levels = format_trade_levels(event, self.config)
        message = (
            f"[{event.triggered_at.isoformat()}] {event.symbol_request} ({event.symbol_actual}) "
            f"{event.signal_source.upper()} {event.signal_label} on {event.timeframe} | "
            f"price={event.price:.5f}{trade_levels} | image={image_path}"
        )
        if self.config.print_alerts:
            print(message)

        if self.config.enable_sound and not self.config.telegram_only and platform.system() == "Windows":
            try:
                import winsound

                winsound.Beep(1200 if event.zone.direction == "bullish" else 800, 300)
            except Exception:
                pass

        if self.has_telegram_credentials:
            self.send_telegram(event, image_path)

    @property
    def has_telegram_credentials(self) -> bool:
        return bool(self.config.telegram_bot_token and self.config.telegram_chat_ids)

    def validate_telegram(self) -> None:
        if self.has_telegram_credentials:
            return
        raise RuntimeError(
            "Telegram alerts are enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID(S) is missing. "
            "Add them to your environment or .env file."
        )

    def _log_telegram_failure(self, event: SignalEvent, reason: str) -> None:
        """Log failed Telegram delivery to a fallback file for manual review/retry."""
        fallback_file = self.config.output_dir / "telegram_fallback.jsonl"
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": event.triggered_at.isoformat(),
            "symbol_request": event.symbol_request,
            "symbol_actual": event.symbol_actual,
            "timeframe": event.timeframe,
            "signal_source": event.signal_source,
            "signal_label": event.signal_label,
            "price": event.price,
            "direction": event.zone.direction,
            "entry_price": event.entry_price,
            "stop_loss": event.stop_loss,
            "take_profit": event.take_profit,
            "telegram_failure_reason": reason,
        }
        with fallback_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        print(f"Telegram fallback logged: {event.symbol_request} {event.timeframe} to {fallback_file}")

    def send_telegram(self, event: SignalEvent, image_path: Path) -> None:
        # If we're in a text-only cooldown window due to repeated SSL errors,
        # skip image upload and send a text-only alert instead.
        if time.time() < getattr(self, "_text_only_until", 0.0):
            fallback_msg = (
                f"ALERT (image uploads temporarily disabled) - {event.symbol_request} ({event.symbol_actual}) {event.signal_label} on {event.timeframe}\n"
                f"Price: {event.price:.5f}\nDirection: {event.zone.direction.title()}\n"
                f"(Image uploads disabled for {int(self._text_only_until - time.time())}s due to repeated SSL errors)"
            )
            try:
                self.send_text_alert(fallback_msg)
                print("In text-only mode due to SSL errors: sent text-only alert.")
                return
            except Exception:
                # fall through to normal handling so failure is logged
                pass
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendPhoto"
        risk_lines = ""
        if event.stop_loss is not None:
            rr = (
                f"{event.risk_reward_ratio:.2f}R"
                if event.risk_reward_ratio is not None
                else "n/a"
            )
            entry_line = (
                f"\nEntry: {event.entry_price:.5f}"
                if event.entry_price is not None
                else ""
            )
            tp1_line = (
                f"\nTP1: {event.take_profit_1:.5f}"
                if event.take_profit_1 is not None
                else ""
            )
            tp2_line = (
                f"\nTP2: {event.take_profit_2:.5f}"
                if event.take_profit_2 is not None
                else ""
            )
            tp3_value = event.take_profit_3 if event.take_profit_3 is not None else event.take_profit
            tp3_line = f"\nTP3: {tp3_value:.5f}" if tp3_value is not None else ""
            target_label, target_value = _current_tp_target(event, self.config)
            target_line = (
                f"\nTarget: {target_label} {target_value:.5f}"
                if target_value is not None
                else ""
            )
            risk_lines = (
                f"{entry_line}"
                f"\nSL: {event.stop_loss:.5f}"
                f"{tp1_line}"
                f"{tp2_line}"
                f"{tp3_line}"
                f"{target_line}"
                f"\nRR: {rr}"
            )
        alert_prefix = "MARKET CHANGE ALERT: " if event.market_change_alert else ""
        explanation = format_signal_explanation(event)
        lesson = f"\nLesson: {event.lesson.strip()}" if event.lesson else ""
        caption = (
            f"{alert_prefix}{event.symbol_request} ({event.symbol_actual})\n"
            f"{event.signal_source.upper()} {event.signal_label or 'Signal'} on {event.timeframe}\n"
            f"Price: {event.price:.5f}\n"
            f"Direction: {event.zone.direction.title()}\n"
            f"{explanation}\n"
            f"{risk_lines}"
            f"{lesson}"
        )
        image_bytes = image_path.read_bytes()
        failures: list[str] = []
        delivered = 0
        for chat_id in self.config.telegram_chat_ids:
            try:
                response = self._post_with_retries(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "disable_notification": str(self.config.telegram_silent).lower(),
                    },
                    files={"photo": (image_path.name, image_bytes, "image/png")},
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                # network-level failures (DNS, connection, timeouts)
                failures.append(f"{chat_id}: network error: {exc}")
                # If the error looks like an SSL EOF, attempt a text-only fallback below after the loop
                continue

            try:
                self.raise_for_telegram_error(response)
                delivered += 1
            except RuntimeError as exc:
                failures.append(f"{chat_id}: {exc}")

        if delivered == 0 and failures:
            # Attempt a text-only fallback to at least deliver the alert without the image.
            fallback_msg = (
                f"ALERT (image upload failed) - {event.symbol_request} ({event.symbol_actual}) {event.signal_label} on {event.timeframe}\n"
                f"Price: {event.price:.5f}\nDirection: {event.zone.direction.title()}\n{risk_lines}\n"
                f"(Image upload failed; logged fallback)"
            )
            try:
                delivered_text = self.send_text_alert(fallback_msg)
            except Exception:
                delivered_text = 0

            if delivered_text > 0:
                print("Image upload failed; sent text-only fallback alert to Telegram.")
                return

            # text fallback didn't deliver either — log and raise
            self._log_telegram_failure(event, " | ".join(failures))
            raise RuntimeError("Telegram delivery failed for all recipients. " + " | ".join(failures))
        if failures:
            print("Telegram delivery warning: " + " | ".join(failures))

    def send_test_message(self) -> int:
        self.validate_telegram()
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        failures: list[str] = []
        delivered = 0
        for chat_id in self.config.telegram_chat_ids:
            try:
                response = self._post_with_retries(
                    url,
                    data={
                        "chat_id": chat_id,
                        "text": "SWIFT watcher is connected and ready to send image alerts.",
                        "disable_notification": str(self.config.telegram_silent).lower(),
                    },
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                failures.append(f"{chat_id}: network error: {exc}")
                continue

            try:
                self.raise_for_telegram_error(response)
                delivered += 1
            except RuntimeError as exc:
                failures.append(f"{chat_id}: {exc}")

        if failures:
            raise RuntimeError("Telegram test failed for one or more recipients. " + " | ".join(failures))
        return delivered

    def send_photo(self, image_path: Path, caption: str = "") -> int:
        """Send a photo alert to configured Telegram chat IDs."""
        self.validate_telegram()
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendPhoto"
        image_bytes = image_path.read_bytes()
        failures: list[str] = []
        delivered = 0

        for chat_id in self.config.telegram_chat_ids:
            try:
                response = self._post_with_retries(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "disable_notification": str(self.config.telegram_silent).lower(),
                    },
                    files={"photo": (image_path.name, image_bytes, "image/png")},
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                failures.append(f"{chat_id}: network error: {exc}")
                continue

            try:
                self.raise_for_telegram_error(response)
                delivered += 1
            except RuntimeError as exc:
                failures.append(f"{chat_id}: {exc}")

        if delivered == 0 and failures:
            raise RuntimeError("Telegram photo delivery failed for all recipients. " + " | ".join(failures))
        if failures:
            print("Telegram delivery warning: " + " | ".join(failures))
        return delivered

    def send_text_alert(self, message: str) -> int:
        """Send a text-only alert (for trade executions, partials, etc.)."""
        if not self.has_telegram_credentials:
            return 0
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        failures: list[str] = []
        delivered = 0
        for chat_id in self.config.telegram_chat_ids:
            try:
                response = self._post_with_retries(
                    url,
                    data={
                        "chat_id": chat_id,
                        "text": message,
                        "disable_notification": str(self.config.telegram_silent).lower(),
                    },
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                failures.append(f"{chat_id}: network error: {exc}")
                continue

            try:
                self.raise_for_telegram_error(response)
                delivered += 1
            except RuntimeError as exc:
                failures.append(f"{chat_id}: {exc}")

        if failures:
            print("Text alert delivery warning: " + " | ".join(failures))
        return delivered

    def send_early_m1_alert(self, event: SignalEvent) -> int:
        """Send early M1 signal alert (text-only, rapid signal detection)."""
        if not self.has_telegram_credentials or event.timeframe != "M1":
            return 0
        
        trade_levels = format_trade_levels(event, self.config)
        alert_prefix = "🟡 EARLY M1 SIGNAL"
        message = (
            f"{alert_prefix}\n"
            f"Symbol: {event.symbol_request} ({event.symbol_actual})\n"
            f"Direction: {event.zone.direction.upper()}\n"
            f"Entry: {event.entry_price:.5f}\n"
            f"Price: {event.price:.5f}{trade_levels}\n"
            f"[M1 Early Entry - Confluence-Ready]\n"
            f"Time: {event.triggered_at.isoformat()}"
        )
        return self.send_text_alert(message)

    def send_document(self, document_path: Path, caption: str = "") -> int:
        self.validate_telegram()
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendDocument"
        failures: list[str] = []
        delivered = 0
        document_bytes = document_path.read_bytes()

        for chat_id in self.config.telegram_chat_ids:
            try:
                response = self._post_with_retries(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "disable_notification": str(self.config.telegram_silent).lower(),
                    },
                    files={
                        "document": (
                            document_path.name,
                            document_bytes,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    timeout=60,
                )
            except requests.exceptions.RequestException as exc:
                failures.append(f"{chat_id}: network error: {exc}")
                continue

            try:
                self.raise_for_telegram_error(response)
                delivered += 1
            except RuntimeError as exc:
                failures.append(f"{chat_id}: {exc}")

        if delivered == 0 and failures:
            record = {
                "timestamp": time.time(),
                "document_path": str(document_path),
                "caption": caption,
                "telegram_failure_reason": " | ".join(failures),
            }
            fallback_file = self.config.output_dir / "telegram_fallback.jsonl"
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            with fallback_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
            print(f"Telegram document delivery fallback logged to {fallback_file}")
            raise RuntimeError("Telegram delivery failed for all recipients. " + " | ".join(failures))
        if failures:
            print("Telegram delivery warning: " + " | ".join(failures))
        return delivered

    @staticmethod
    def raise_for_telegram_error(response: requests.Response) -> None:
        if response.ok:
            return

        description = ""
        try:
            payload = response.json()
            description = payload.get("description", "")
        except ValueError:
            description = response.text.strip()

        if response.status_code == 401:
            raise RuntimeError(
                "Telegram rejected the bot token with 401 Unauthorized. "
                "Generate a fresh token in BotFather and update TELEGRAM_BOT_TOKEN in .env."
            )

        if response.status_code == 400:
            raise RuntimeError(
                "Telegram rejected the request. This usually means TELEGRAM_CHAT_ID is wrong or the bot "
                f"has not been started from that chat yet. Telegram said: {description or 'Bad Request'}"
            )

        if response.status_code == 403 and "bot can't send messages to the bot" in description.lower():
            raise RuntimeError(
                "Telegram says the current TELEGRAM_CHAT_ID belongs to a bot. "
                "Use your own personal Telegram user/chat ID instead, then start a chat with your bot and send /start."
            )

        raise RuntimeError(
            f"Telegram request failed with HTTP {response.status_code}. "
            f"{description or response.reason}"
        )
