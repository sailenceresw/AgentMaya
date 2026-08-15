"""Timeframe parsing, in one place.

Four modules previously each carried their own ``_TF_MS = {"1m": ..., "5m":
..., "15m": ...}`` literal. Any timeframe outside those three silently fell
back to a default of one minute in some of them, so configuring the agent for
30m or 1h would have produced correct-looking candles on a scheduler that
still believed a bar closed every 60 seconds.

Deriving the interval instead of looking it up removes the whole class of
problem: anything ``parse_timeframe`` accepts, every module handles.
"""
from __future__ import annotations

_UNITS = {"m": 1, "h": 60, "d": 1440}


def parse_timeframe(tf: str) -> int:
    """'15m' -> 15, '1h' -> 60, '4h' -> 240, '1d' -> 1440. Whole minutes."""
    tf = (tf or "").strip().lower()
    if not tf or tf[-1] not in _UNITS or not tf[:-1].isdigit():
        raise ValueError(f"unrecognised timeframe {tf!r} (expected e.g. '15m', '1h', '4h')")
    value = int(tf[:-1])
    if value < 1:
        raise ValueError(f"timeframe {tf!r} must be at least 1 unit")
    return value * _UNITS[tf[-1]]


def timeframe_ms(tf: str, default: int = 60_000) -> int:
    """Bar interval in milliseconds, falling back to ``default`` if unparseable."""
    try:
        return parse_timeframe(tf) * 60_000
    except ValueError:
        return default


def label_for(minutes: int) -> str:
    """Inverse of :func:`parse_timeframe`: 240 -> '4h'."""
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"
