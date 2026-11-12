"""High-impact news blackout calendar (spec §7.4).

During a blackout window (±N minutes around a scheduled high-impact event such
as CPI, FOMC, NFP, GDP) the risk layer refuses new entries; open positions are
still managed normally.

The calendar is loaded from a JSON file (``MAYA_NEWS_CALENDAR``) that is a list
of either ISO-8601 timestamps or ``{"time": ..., "name": ...}`` objects, e.g.:

    [
      {"time": "2026-01-14T13:30:00Z", "name": "US CPI"},
      "2026-01-29T19:00:00Z"
    ]
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone


def _to_ms(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass
class NewsCalendar:
    events: list[tuple[int, str]]      # (timestamp_ms, name), sorted
    window_ms: int

    @classmethod
    def from_events(cls, events, blackout_minutes: int = 15) -> "NewsCalendar":
        parsed: list[tuple[int, str]] = []
        for item in events:
            if isinstance(item, dict):
                parsed.append((_to_ms(item["time"]), str(item.get("name", "event"))))
            else:
                parsed.append((_to_ms(item), "event"))
        parsed.sort()
        return cls(parsed, blackout_minutes * 60_000)

    @classmethod
    def from_file(cls, path: str, blackout_minutes: int = 15) -> "NewsCalendar":
        with open(path) as fh:
            data = json.load(fh)
        return cls.from_events(data, blackout_minutes)

    def in_blackout(self, ts_ms: int) -> tuple[bool, str | None]:
        for ets, name in self.events:
            if abs(ts_ms - ets) <= self.window_ms:
                return True, name
            if ets - ts_ms > self.window_ms:
                break  # events are sorted; no later event can match earlier ts
        return False, None
