"""Optional LangSmith tracing for the LangGraph pipeline.

LangChain/LangGraph trace automatically when the standard ``LANGSMITH_*``
environment variables are set *before* the runnables execute. :func:`configure`
maps Maya's settings onto those variables, gated by ``MAYA_LANGSMITH_TRACING``,
so turning it on is a deliberate, documented choice and nothing reaches the
network until then.

Tracing is best-effort: the LangSmith client batches runs on a background
thread and swallows delivery failures, so an unreachable endpoint never stalls
or crashes trading — it just means no traces arrive.
"""
from __future__ import annotations

import os

from loguru import logger

from maya.config import Settings


def configure(settings: Settings) -> bool:
    """Enable LangSmith tracing from settings. Returns True if it was turned on.

    Call once at startup, before the graph is built. A no-op (and no network)
    unless ``langsmith_tracing`` is set and an API key is present.
    """
    if not settings.langsmith_tracing:
        return False
    if not settings.langsmith_api_key:
        logger.warning("MAYA_LANGSMITH_TRACING is on but no LANGSMITH_API_KEY is "
                       "set — tracing stays off.")
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint

    logger.info("LangSmith tracing enabled → project '{}'", settings.langsmith_project)
    return True
