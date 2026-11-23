"""Shared test setup.

Tests never touch the network. A developer's .env may switch LangSmith tracing
on; the process environment wins over .env in pydantic-settings, so pinning it
off here keeps graph tests from sending traces."""
import os

os.environ["MAYA_LANGSMITH_TRACE"] = "off"
