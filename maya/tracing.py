"""Opt-in LangSmith tracing for the LLM veto and the LangGraph pipeline.

Maya runs on LangGraph and calls Claude through LangChain, so LangSmith can
record both without any extra instrumentation. What it records is decided by
``MAYA_LANGSMITH_TRACE``:

``off``   (default) nothing is sent.
``llm``   only the LLM veto calls. They happen on trades the rules already
          approved, so a handful a day.
``graph`` every ``graph.invoke`` as well, one trace per closed candle. On 1m
          that is about 1,440 traces a day, so keep it for short debugging
          runs.

Tracing is switched on per call with ``langsmith.tracing_context`` instead of
the global ``LANGSMITH_TRACING`` env var. That way the setting here is the
only switch, and a leftover env var cannot start tracing every candle.

Tracing must never affect trading. If the client cannot be built, the call
runs untraced and a warning is logged once.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Literal

from loguru import logger

from maya.config import Settings

Scope = Literal["llm", "graph"]

# Which scopes each MAYA_LANGSMITH_TRACE value covers.
_COVERS: dict[str, frozenset[str]] = {
    "off": frozenset(),
    "llm": frozenset({"llm"}),
    "graph": frozenset({"llm", "graph"}),
}

# One client per (endpoint, key, workspace). The client batches and sends
# traces on a background thread, so building it per call would leak threads.
_clients: dict[tuple[str, str, str], Any] = {}
_warned = False


def _client(settings: Settings):
    key = (settings.langsmith_endpoint, settings.langsmith_api_key,
           settings.langsmith_workspace_id)
    if key not in _clients:
        from langsmith import Client

        _clients[key] = Client(
            api_url=settings.langsmith_endpoint,
            api_key=settings.langsmith_api_key,
            workspace_id=settings.langsmith_workspace_id or None,
        )
    return _clients[key]


def tracing_enabled(settings: Settings, scope: Scope) -> bool:
    """True when ``scope`` should be sent to LangSmith under these settings."""
    return bool(settings.langsmith_api_key) and scope in _COVERS[settings.langsmith_trace]


def trace(settings: Settings, scope: Scope, *, tags: list[str] | None = None,
          metadata: dict[str, Any] | None = None) -> AbstractContextManager:
    """Context manager that traces the wrapped LangChain/LangGraph call, or not.

    When the scope is not covered it returns ``tracing_context(enabled=False)``,
    which also overrides a stray global ``LANGSMITH_TRACING=true``.
    """
    global _warned
    try:
        from langsmith.run_helpers import tracing_context
    except Exception:  # noqa: BLE001 - langsmith missing: nothing to trace or disable
        return nullcontext()

    if not tracing_enabled(settings, scope):
        return tracing_context(enabled=False)
    try:
        return tracing_context(
            enabled=True,
            project_name=settings.langsmith_project,
            client=_client(settings),
            tags=["maya", settings.mode.value, *(tags or [])],
            metadata={"symbol": settings.symbol, "timeframe": settings.timeframe,
                      "venue": settings.venue.value, **(metadata or {})},
        )
    except Exception as exc:  # noqa: BLE001 - tracing must never stop trading
        if not _warned:
            logger.warning("LangSmith tracing unavailable, running untraced: {}", exc)
            _warned = True
        return tracing_context(enabled=False)


def log_startup(settings: Settings) -> None:
    """One log line saying what, if anything, goes to LangSmith."""
    if settings.langsmith_trace == "off":
        return
    if not settings.langsmith_api_key:
        logger.warning("MAYA_LANGSMITH_TRACE={} but LANGSMITH_API_KEY is empty; "
                       "tracing stays off", settings.langsmith_trace)
        return
    logger.info("LangSmith tracing ON: scope={} project={} endpoint={}",
                settings.langsmith_trace, settings.langsmith_project,
                settings.langsmith_endpoint)
    if settings.langsmith_trace == "graph":
        logger.warning("LangSmith scope=graph sends one trace per candle "
                       "(~1,440/day on 1m). Use it for short runs.")
