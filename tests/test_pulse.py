"""MetaMask Pulse as LLM context: cached, bounded, and never on the hot path."""
from __future__ import annotations

import json
import subprocess

import pytest

from maya.config import RunMode, Settings
from maya.strategy.llm_filter import LLMFilter, PulseContext


@pytest.fixture
def pulse(monkeypatch) -> PulseContext:
    monkeypatch.setattr("maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm")
    return PulseContext(Settings(mode=RunMode.PAPER, pulse_enabled=True, pulse_ttl_s=900))


def _replay(monkeypatch, *payloads):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        payload = payloads[min(len(calls), len(payloads)) - 1]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_pulse_targets_the_hyperliquid_market(pulse, monkeypatch):
    calls = _replay(monkeypatch, {"ok": True, "data": {"summary": "BTC funding flipped"}})
    assert "BTC funding flipped" in pulse.get("BTCUSDT")
    assert calls[0][1:5] == ["pulse", "asset", "--hl-perps-market", "BTC"]


def test_pulse_is_cached_for_its_ttl(pulse, monkeypatch):
    calls = _replay(monkeypatch, {"ok": True, "data": {"summary": "x"}})
    pulse.get("BTCUSDT")
    pulse.get("BTCUSDT")
    assert len(calls) == 1


def test_pulse_failure_is_cached_and_returns_none(pulse, monkeypatch):
    calls = _replay(monkeypatch, {"ok": False, "error": {"code": "PULSE_NOT_FOUND",
                                                        "message": "no digest"}})
    assert pulse.get("BTCUSDT") is None
    assert pulse.get("BTCUSDT") is None
    assert len(calls) == 1   # a failing endpoint is not retried every candle


def test_pulse_text_is_bounded(pulse, monkeypatch):
    _replay(monkeypatch, {"ok": True, "data": {"summary": "z" * 50_000}})
    assert len(pulse.get("BTCUSDT")) <= 3000


def test_pulse_stays_off_without_the_llm_layer(monkeypatch):
    monkeypatch.setattr("maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm")
    f = LLMFilter(Settings(mode=RunMode.PAPER, pulse_enabled=True, llm_enabled=False))
    assert f._pulse is None
