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
    "already passed a rules-based order-flow strategy and a confluence score. "
    "Your ONLY job is to VETO trades that look clearly unsafe or contradictory. "
    "You never originate trades. Respond with strict JSON: "
    '{"approve": true|false, "reason": "<short>"}. Default to approve unless '
    "there is a clear red flag."
)


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
        try:
            msg = self._client.messages.create(
                model=self.settings.llm_model,
                max_tokens=256,
                system=_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(text)
            return bool(data.get("approve", True)), str(data.get("reason", ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM review failed, approving by default: {}", exc)
            return True, f"llm_error:{exc}"
