"""LLM veto: request shape, schema-enforced verdicts, and fail-open paths.

No network: request options are checked on the real ChatAnthropic payload
(built offline) and replies come from a fake chat model."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from maya.config import Settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from maya.strategy.llm_filter import LLMFilter, build_chat_model
from maya.strategy.models import TradeIntent


def _intent():
    return TradeIntent("BTCUSDT", "short", "liquidity_sweep_choch", entry_price=66800,
                       stop_loss=67388, take_profit_1=63700, take_profit_2=62000,
                       confluence_score=8, expected_rr=5.3)


def _sig():
    return SimpleNamespace(price=66800.0, cvd_1m=-12.0, cvd_divergence="bearish",
                           amt_phase="distribution", funding_rate=0.0001,
                           volume=120.0, avg_volume=80.0)


class _Model:
    """Stands in for ChatAnthropic: records each invoke, returns a canned reply."""

    def __init__(self, reply):
        self.calls = []
        self.reply = reply

    def invoke(self, messages, config=None):
        self.calls.append((messages, config))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _msg(text="", stop="end_turn"):
    return AIMessage(content=[{"type": "thinking", "thinking": ""},
                              {"type": "text", "text": text}],
                     response_metadata={"stop_reason": stop})


def _filter(reply, **kw) -> tuple[LLMFilter, _Model]:
    f = LLMFilter(Settings(llm_enabled=False, **kw))
    model = _Model(reply)
    f.enabled, f._llm = True, model
    return f, model


def _payload(**kw) -> dict:
    """The exact request ChatAnthropic would send. Built offline: no network."""
    llm = build_chat_model(Settings(anthropic_api_key="sk-test", **kw))
    return llm._get_request_payload([SystemMessage("s"), HumanMessage("h")])


def test_defaults_to_opus_5_5_with_schema_low_effort_and_fallbacks():
    req = _payload()
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["format"]["schema"]["required"] == ["approve", "reason"]
    assert req["fallbacks"] == "default"
    assert req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["max_tokens"] >= 4096          # thinking tokens count against it
    # Thinking cannot be switched off on this model; it must be adaptive,
    # never a fixed budget_tokens (rejected on Opus 5.x).
    assert req.get("thinking", {"type": "adaptive"})["type"] == "adaptive"


def test_review_sends_system_prompt_trade_payload_and_trace_tags():
    f, model = _filter(_msg(json.dumps({"approve": True, "reason": "clean setup"})))
    assert f.review(_intent(), _sig()) == (True, "clean setup")
    messages, config = model.calls[0]
    assert isinstance(messages[0], SystemMessage) and "veto" in messages[0].content
    sent = json.loads(messages[1].content)
    assert sent["intent"]["symbol"] == "BTCUSDT"
    assert sent["signal"]["volume_vs_avg"] == 1.5
    assert config["run_name"] == "maya_llm_veto"
    assert "llm_veto" in config["tags"]
    assert config["metadata"]["setup"] == "liquidity_sweep_choch"


def test_a_veto_is_returned():
    f, _ = _filter(_msg(json.dumps({"approve": False, "reason": "funding against"})))
    assert f.review(_intent(), _sig()) == (False, "funding against")


def test_models_without_fallback_support_get_no_fallback_params():
    req = _payload(llm_model="claude-haiku-5-5")
    assert "fallbacks" not in req and not req.get("betas")


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
