"""Data-feed watchdog: halts on silence, never on a single slow candle."""
from __future__ import annotations

from maya.config import RiskLimits, RunMode, Settings
from maya.data.watchdog import FeedWatchdog
from maya.risk.manager import RiskManager, SystemState


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _dog(timeframe="1m"):
    clock, fired = _Clock(), []
    dog = FeedWatchdog(timeframe, on_stale=fired.append, clock=clock)
    return dog, clock, fired


def test_regular_candles_never_fire():
    dog, clock, fired = _dog()
    for _ in range(10):
        clock.t += 60
        dog.beat()
        assert not dog.check()
    assert fired == []


def test_silence_beyond_interval_plus_grace_fires_once():
    dog, clock, fired = _dog()
    dog.beat()
    clock.t += 60 + RiskLimits.DATA_STALE_SECONDS + 1
    assert dog.check()
    assert not dog.check()            # once, not every poll
    assert len(fired) == 1


def test_first_candle_gets_a_whole_extra_interval():
    """At startup the feed sleeps until the next close, so the first candle
    can take most of an interval — that is not a stall."""
    dog, clock, fired = _dog("1h")
    clock.t += 3600 + 60             # 3660s: over one allowance (3630), under two
    assert not dog.check()
    clock.t += 3601                  # 7261s: past two allowances (7260)
    assert dog.check()


def test_firing_halts_new_trades_through_the_risk_manager():
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000), 1000)
    clock = _Clock()
    dog = FeedWatchdog("1m", clock=clock,
                       on_stale=lambda gap: rm.check_kill_switch(0, data_stale=True))
    dog.beat()
    clock.t += 1000
    dog.check()
    assert rm.account.state is SystemState.HALTED
    assert any("data feed" in r for r in rm.account.halt_reasons)
