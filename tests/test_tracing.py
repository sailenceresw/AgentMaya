"""LangSmith tracing: scope switch, no client without a key, never blocks.

No network: a real langsmith Client is never built with a real key, and the
traced call is a local function."""
from __future__ import annotations

import pytest
from langsmith.run_helpers import get_tracing_context

import maya.tracing as tracing
from maya.config import Settings


def _s(**kw) -> Settings:
    return Settings(**kw)


@pytest.mark.parametrize("trace,key,llm,graph", [
    ("off", "lsv2_x", False, False),
    ("llm", "lsv2_x", True, False),
    ("graph", "lsv2_x", True, True),
    ("graph", "", False, False),      # no key: nothing is ever sent
])
def test_scope_switch(trace, key, llm, graph):
    s = _s(langsmith_trace=trace, langsmith_api_key=key)
    assert tracing.tracing_enabled(s, "llm") is llm
    assert tracing.tracing_enabled(s, "graph") is graph


def test_uncovered_scope_disables_tracing_even_if_env_says_on(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with tracing.trace(_s(langsmith_trace="llm", langsmith_api_key="lsv2_x"), "graph"):
        assert get_tracing_context()["enabled"] is False


def test_off_never_builds_a_client(monkeypatch):
    built = []
    monkeypatch.setattr(tracing, "_client", lambda s: built.append(s))
    with tracing.trace(_s(), "llm"):
        pass
    assert built == []


def test_covered_scope_sets_project_tags_and_client(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(tracing, "_client", lambda s: sentinel)
    s = _s(langsmith_trace="llm", langsmith_api_key="lsv2_x", langsmith_project="maya-dev")
    with tracing.trace(s, "llm", tags=["veto"]):
        ctx = get_tracing_context()
        assert ctx["enabled"] is True
        assert ctx["project_name"] == "maya-dev"
        assert ctx["client"] is sentinel
        assert {"maya", "sim", "veto"} <= set(ctx["tags"])
        assert ctx["metadata"]["symbol"] == "BTCUSDT"


def test_client_failure_runs_untraced(monkeypatch):
    def boom(s):
        raise RuntimeError("bad endpoint")
    monkeypatch.setattr(tracing, "_client", boom)
    with tracing.trace(_s(langsmith_trace="graph", langsmith_api_key="lsv2_x"), "graph"):
        assert get_tracing_context()["enabled"] is False


def test_client_is_built_once_per_key(monkeypatch):
    import langsmith

    made = []

    class _FakeClient:
        def __init__(self, **kw):
            made.append(kw)

    monkeypatch.setattr(langsmith, "Client", _FakeClient)
    monkeypatch.setattr(tracing, "_clients", {})
    s = _s(langsmith_trace="llm", langsmith_api_key="lsv2_x",
           langsmith_workspace_id="ws-1")
    tracing._client(s)
    tracing._client(s)
    assert len(made) == 1
    assert made[0]["workspace_id"] == "ws-1"
    assert made[0]["api_url"] == "https://api.smith.langchain.com"
