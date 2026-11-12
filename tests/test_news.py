"""Tests for the news-event blackout (spec §7.4)."""
from maya.config import RunMode, Settings
from maya.risk.manager import RiskManager
from maya.risk.news import NewsCalendar
from maya.strategy.models import TradeIntent


def _intent():
    return TradeIntent("BTCUSDT", "long", "value_area_absorption", 100, 95, 120,
                       125, confluence_score=8, expected_rr=5.0)


def test_news_calendar_window():
    cal = NewsCalendar.from_events([{"time": "2026-01-14T13:30:00Z", "name": "US CPI"}],
                                   blackout_minutes=15)
    event_ms = cal.events[0][0]
    assert cal.in_blackout(event_ms)[0] is True
    assert cal.in_blackout(event_ms - 10 * 60_000)[0] is True   # 10 min before
    assert cal.in_blackout(event_ms + 14 * 60_000)[0] is True   # 14 min after
    assert cal.in_blackout(event_ms + 20 * 60_000)[0] is False  # 20 min after


def test_risk_rejects_entries_during_blackout():
    cal = NewsCalendar.from_events([{"time": 1_700_000_000_000, "name": "FOMC"}],
                                   blackout_minutes=15)
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000), 1000, news_calendar=cal)
    rm.roll_periods(1_700_000_000_000)
    # inside the blackout window -> rejected
    d = rm.screen(_intent(), now_ts=1_700_000_000_000)
    assert d.approved is False and "news blackout" in d.reason
    # well outside -> allowed
    d2 = rm.screen(_intent(), now_ts=1_700_000_000_000 + 3_600_000)
    assert d2.approved is True
