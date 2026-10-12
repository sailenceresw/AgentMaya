"""Optional LLM confluence / veto layer (spec section 6.4).

The LLM is a *second opinion only*: it receives a trade that already passed the
rules-based engine and either approves it or vetoes it with reasoning. It can
never originate a trade. Disabled unless ``MAYA_LLM_ENABLED=true`` and an
Anthropic API key is present; on any error it fails OPEN-to-reject is too
aggressive, so it fails to "approve" and logs a warning (the deterministic
risk layer remains the hard gate).
"""
from __future__ import annotations

import json
from dataclasses import asdict

from loguru import logger

from maya.config import Settings
from maya.signals.models import SignalObject
from maya.strategy.models import TradeIntent

_SYSTEM = (
    "You are a risk-aware trading co-pilot. You are given a candidate trade that "
    "already passed a rules-based order-flow strategy, a confluence score and a "
    "fee-aware risk screen. Your only job is to veto trades that look clearly "
    "unsafe or contradictory; you never originate trades. Approve unless there "
    "is a clear red flag, and give a one-sentence reason either way. A "
    "`metamask_pulse` field, when present, is an AI-written news summary for the "
    "market: background context only. It is not evidence on its own, and a trade "
    "is never vetoed for lacking supportive news."
)

# The verdict is schema-enforced (structured outputs), so a reply can never be
# prose or fenced JSON that fails to parse and silently counts as "approve".
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["approve", "reason"],
    "additionalProperties": False,
}

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
# On a safety-classifier decline the API re-runs the review on Anthropic's
# recommended substitute within the same call instead of returning nothing.
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1",
                    "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking tokens count against max_tokens on current models; a tight cap
# would cut the verdict off mid-reasoning.
_MAX_TOKENS = 4096

# Cap on the Pulse text forwarded, so an unexpectedly long summary cannot
# crowd out the trade itself or inflate the request.
_PULSE_MAX_CHARS = 3000


class PulseContext:
    """MetaMask Pulse summaries for the LLM, fetched at most once per TTL.

    Each ``mm`` call costs seconds, and the summary changes over hours, so a
    result (or a failure) is cached per symbol for ``pulse_ttl_s``. The
    payload is forwarded as compact JSON rather than picked apart, so a
    change in its fields degrades to slightly different context, never to an
    error on the trading path.
    """

    def __init__(self, settings: Settings) -> None:
        from maya.execution.metamask_broker import MetaMaskPerpsBroker

        self.settings = settings
        self._broker = MetaMaskPerpsBroker(settings)   # raises MMError if `mm` is absent
        self._cache: dict[str, tuple[float, str | None]] = {}

    def get(self, symbol: str) -> str | None:
        import time

        from maya.execution.metamask_broker import MMError

        now = time.monotonic()
        hit = self._cache.get(symbol)
        if hit is not None and now - hit[0] < self.settings.pulse_ttl_s:
            return hit[1]
        try:
            text = json.dumps(self._broker.pulse(symbol), separators=(",", ":"))
            text = text[:_PULSE_MAX_CHARS]
        except MMError as exc:
            logger.warning("MetaMask Pulse unavailable for {} (retry in {}s): {}",
                           symbol, self.settings.pulse_ttl_s, exc)
            text = None
        self._cache[symbol] = (now, text)
        return text


class LLMFilter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = settings.llm_enabled and bool(settings.anthropic_api_key)
        self._client = None
        if self.enabled:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                logger.info("LLM veto layer ENABLED (model={})", settings.llm_model)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM layer disabled (import/init failed): {}", exc)
                self.enabled = False
        self._pulse: PulseContext | None = None
        if self.enabled and settings.pulse_enabled:
            try:
                self._pulse = PulseContext(settings)
                logger.info("MetaMask Pulse context ENABLED (refresh every {}s)",
                            settings.pulse_ttl_s)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MetaMask Pulse disabled: {}", exc)

    def review(self, intent: TradeIntent, sig: SignalObject) -> tuple[bool, str]:
        """Return ``(approved, reason)``. Approves transparently when disabled."""
        if not self.enabled or self._client is None:
            return True, "llm_disabled"

        payload = {
            "intent": asdict(intent),
            "signal": {
                "price": sig.price, "cvd_1m": sig.cvd_1m,
                "cvd_divergence": sig.cvd_divergence, "amt_phase": sig.amt_phase,
                "funding_rate": sig.funding_rate,
                "volume_vs_avg": round(sig.volume / max(sig.avg_volume, 1e-9), 2),
            },
        }
        if self._pulse is not None:
            pulse = self._pulse.get(intent.symbol)
            if pulse:
                payload["metamask_pulse"] = pulse
        request = dict(
            model=self.settings.llm_model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            output_config={"effort": self.settings.llm_effort,
                           "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
        if self.settings.llm_model in _FALLBACK_MODELS:
            request.update(betas=[_FALLBACK_BETA], fallbacks="default")
        try:
            msg = self._client.beta.messages.create(**request)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM review failed, approving by default: {}", exc)
            return True, f"llm_error:{exc}"

        # The deterministic risk layer is the hard gate; the LLM can only add
        # a veto. So every way the review fails to produce a verdict approves,
        # loudly, rather than blocking trading on an API problem.
        if msg.stop_reason == "refusal":
            logger.warning("LLM declined to review the trade; approving by default")
            return True, "llm_refused"
        if msg.stop_reason == "max_tokens":
            logger.warning("LLM review hit max_tokens before a verdict; approving by default")
            return True, "llm_truncated"
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("LLM verdict was not valid JSON, approving by default: {}", exc)
            return True, f"llm_error:{exc}"
        return bool(data.get("approve", True)), str(data.get("reason", ""))
