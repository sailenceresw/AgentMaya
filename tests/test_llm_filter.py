"""LLM veto: request shape, schema-enforced verdicts, and fail-open paths.

No network: the Anthropic client is replaced with a recorder."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from maya.config import Settings
from maya.strategy.llm_filter import LLMFilter
from maya.strategy.models import TradeIntent


def _intent():
    return TradeIntent("BTCUSDT", "short", "liquidity_sweep_choch", entry_price=66800,
                       stop_loss=67388, take_profit_1=63700, take_profit_2=62000,
                       confluence_score=8, expected_rr=5.3)


def _sig():
    return SimpleNamespace(price=66800.0, cvd_1m=-12.0, cvd_divergence="bearish",
                           amt_phase="distribution", funding_rate=0.0001,
                           volume=120.0, avg_volume=80.0)


class _Client:
    def __init__(self, reply):
        self.calls = []
        self.reply = reply
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _msg(text="", stop="end_turn"):
    return SimpleNamespace(stop_reason=stop,
                           content=[SimpleNamespace(type="thinking", thinking=""),
                                    SimpleNamespace(type="text", text=text)])


def _filter(reply, **kw) -> tuple[LLMFilter, _Client]:
    f = LLMFilter(Settings(llm_enabled=False, **kw))
    client = _Client(reply)
    f.enabled, f._client = True, client
    return f, client


def test_defaults_to_opus_5_5_with_schema_low_effort_and_fallbacks():
    f, client = _filter(_msg(json.dumps({"approve": True, "reason": "clean setup"})))
    assert f.review(_intent(), _sig()) == (True, "clean setup")
    req = client.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["fallbacks"] == "default"
    assert req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["max_tokens"] >= 4096          # thinking tokens count against it
    assert "thinking" not in req              # cannot be disabled on this model


def test_a_veto_is_returned():
    f, _ = _filter(_msg(json.dumps({"approve": False, "reason": "funding against"})))
    assert f.review(_intent(), _sig()) == (False, "funding against")


def test_models_without_fallback_support_get_no_fallback_params():
    f, client = _filter(_msg(json.dumps({"approve": True, "reason": "ok"})),
                        llm_model="claude-haiku-5-5")
    f.review(_intent(), _sig())
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]


@pytest.mark.parametrize("reply,reason", [
    (_msg(stop="refusal"), "llm_refused"),
    (_msg(stop="max_tokens"), "llm_truncated"),
    (_msg("not json"), "llm_error"),
    (RuntimeError("network down"), "llm_error"),
])
def test_every_failure_approves_because_the_risk_layer_is_the_hard_gate(reply, reason):
    f, _ = _filter(reply)
    approved, why = f.review(_intent(), _sig())
    assert approved and why.startswith(reason)


def test_disabled_filter_never_calls_the_api():
    f = LLMFilter(Settings(llm_enabled=False))
    assert f.review(_intent(), _sig()) == (True, "llm_disabled")
