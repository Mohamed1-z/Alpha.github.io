"""AI-based signal confidence review.

Sends the current setup (signal levels, recent OHLC candles, HTF bias) to Claude
and gets back a structured confidence verdict before a trade is executed.

This is advisory only: it cannot predict price. It evaluates whether a setup looks
structurally sound given the data it's shown -- the same kind of sanity check an
experienced trader does manually before pulling the trigger. It's an additional
filter on top of your existing gates (daily loss breaker, correlation cap, news
blackout, TP1-likelihood check), not a replacement for any of them.

Wiring: called from SwiftWatcher._execute_order_if_enabled() in watcher.py, gated
by config.ai_review_enabled. Requires ANTHROPIC_API_KEY to be set (e.g. in your .env
file, following the same convention as TELEGRAM_BOT_TOKEN in config.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from swift_scanner.config import ScannerConfig
    from swift_scanner.mt5_client import MT5Client
    from swift_scanner.models import SignalEvent


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

_SYSTEM_PROMPT = (
    "You are a risk-review assistant for an algorithmic trading system. You will be "
    "shown a proposed trade setup (a crossover-based signal with entry/stop/targets), "
    "recent price candles, and the higher-timeframe bias. Judge only whether the setup "
    "looks structurally sound based on the data given -- e.g. is the stop placed behind "
    "a sensible structural level, does the entry direction fight or align with recent "
    "momentum and HTF bias, is price already extended into the target zone, does recent "
    "volatility suggest the stop is too tight. You cannot predict where price will go "
    "next and should not pretend to. "
    "Respond with ONLY a single JSON object, no prose, no markdown fences, in exactly "
    "this shape: "
    '{"confidence": <integer 0-100>, "verdict": "approve" or "block", "reason": '
    '"<one short sentence>"}'
)


@dataclass
class ReviewResult:
    confidence: int
    verdict: str  # "approve" | "block"
    reason: str
    ok: bool  # False if this is a fallback result from a failed/unparseable API call

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"


def _build_user_payload(
    event: "SignalEvent",
    recent_candles: list[dict],
    htf_bias: str | None,
) -> str:
    setup = {
        "symbol": event.symbol_actual or event.symbol_request,
        "timeframe": event.timeframe,
        "direction": event.zone.direction,
        "signal_label": event.signal_label,
        "entry_price": event.entry_price,
        "stop_loss": event.stop_loss,
        "take_profit_1": event.take_profit_1,
        "take_profit_2": event.take_profit_2,
        "take_profit_3": event.take_profit_3,
        "risk_reward_ratio": event.risk_reward_ratio,
        "htf_bias_h4": htf_bias or "unknown/ranging",
        "recent_candles_oldest_to_newest": recent_candles,
    }
    return json.dumps(setup, default=str)


def _parse_verdict(raw_text: str) -> ReviewResult | None:
    text = raw_text.strip()
    # Strip accidental markdown fences even though the prompt asks Claude not to use them.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(text)
        confidence = int(data["confidence"])
        verdict = str(data["verdict"]).strip().lower()
        reason = str(data.get("reason", "")).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    confidence = max(0, min(100, confidence))
    if verdict not in {"approve", "block"}:
        verdict = "approve" if confidence >= 70 else "block"
    return ReviewResult(confidence=confidence, verdict=verdict, reason=reason, ok=True)


def review_signal(
    event: "SignalEvent",
    mt5_client: "MT5Client",
    config: "ScannerConfig",
    htf_bias: str | None = None,
) -> ReviewResult:
    """Ask Claude to score confidence in this setup. Returns a ReviewResult.

    On any failure (missing key, network error, timeout, bad response), returns a
    fallback ReviewResult with ok=False. The caller (_execute_order_if_enabled) decides
    whether that fallback approves or blocks the trade via config.ai_review_fail_open --
    this function itself never raises for ordinary API/network problems.
    """
    api_key = config.anthropic_api_key
    if not api_key:
        return ReviewResult(
            confidence=0,
            verdict="approve" if config.ai_review_fail_open else "block",
            reason="ANTHROPIC_API_KEY not set -- AI review skipped",
            ok=False,
        )

    symbol = event.symbol_actual or event.symbol_request
    candle_count = max(1, int(getattr(config, "ai_review_max_candles", 15)))
    try:
        recent_candles = mt5_client.get_recent_candles(symbol, event.timeframe, candle_count)
    except Exception:
        recent_candles = []

    user_content = _build_user_payload(event, recent_candles, htf_bias)

    try:
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": config.ai_review_model,
                "max_tokens": 300,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        text_blocks = [
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        ]
        raw_text = "\n".join(text_blocks).strip()
    except Exception as exc:
        return ReviewResult(
            confidence=0,
            verdict="approve" if config.ai_review_fail_open else "block",
            reason=f"AI review request failed ({exc}) -- {'allowed' if config.ai_review_fail_open else 'blocked'} by fail-open policy",
            ok=False,
        )

    parsed = _parse_verdict(raw_text)
    if parsed is None:
        return ReviewResult(
            confidence=0,
            verdict="approve" if config.ai_review_fail_open else "block",
            reason=f"AI review returned unparseable output -- {'allowed' if config.ai_review_fail_open else 'blocked'} by fail-open policy",
            ok=False,
        )

    return parsed
