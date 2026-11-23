"""LangSmith tracing configuration (maya.observability) — no network touched.

configure() only maps settings onto the LANGSMITH_* env vars LangChain reads;
it never connects. These tests keep tracing off by default and make sure an
enabled-but-unconfigured state fails safe.
"""
import os

from maya.config import Settings
from maya.observability import configure

_KEYS = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT")


def _settings(monkeypatch, **kw):
    # Hermetic: ignore any local .env, and register the env vars so monkeypatch
    # removes whatever configure() sets on teardown (no leak into other tests).
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    return Settings(_env_file=None, **kw)


def test_disabled_is_a_noop(monkeypatch):
    assert configure(_settings(monkeypatch, langsmith_tracing=False)) is False
    assert "LANGSMITH_TRACING" not in os.environ


def test_enabled_without_a_key_stays_off(monkeypatch):
    # Fails safe rather than exporting a half-configured tracer.
    assert configure(_settings(monkeypatch, langsmith_tracing=True)) is False
    assert "LANGSMITH_TRACING" not in os.environ


def test_enabled_exports_langchain_env(monkeypatch):
    s = _settings(monkeypatch, langsmith_tracing=True, langsmith_project="maya-test")
    s.langsmith_api_key = "lsv2_dummy_for_test"   # set by attr; the alias blocks by-name init
    assert configure(s) is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_dummy_for_test"
    assert os.environ["LANGSMITH_PROJECT"] == "maya-test"


def test_tracing_is_off_by_default(monkeypatch):
    assert _settings(monkeypatch).langsmith_tracing is False
