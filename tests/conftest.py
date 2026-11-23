"""Shared test setup."""
from __future__ import annotations

import pytest

from maya.config import Settings


@pytest.fixture(autouse=True)
def _ignore_local_dotenv(monkeypatch):
    """Tests must see the built-in defaults, not the developer's ``.env``.

    ``Settings`` loads ``.env`` from the working directory. With a real one
    present (``MAYA_MODE=testnet``, ``MAYA_VENUE=hyperliquid``) nine tests
    that assert on defaults failed, and tests were being configured by a file
    that also holds a wallet key.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
