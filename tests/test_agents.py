"""Tests for the desk: Atlas, Echo, Flow, Sentinel and Maya's conference."""
import pytest

from maya.agents import atlas, desk, echo, flow, sentinel
from maya.agents.models import BEARISH, BLOCK, BULLISH, NEUTRAL, Conference, Finding
from maya.config import RiskLimits, RunMode, Settings
from maya.data.models import Candle
from maya.risk.manager import RiskManager
from maya.signals.models import Absorption, SignalObject

MIN = 60_000


def _c(i: int, o: float, h: float, lo: float, c: float, v: float = 10.0) -> Candle:
    return Candle(timestamp=i * MIN, open=o, high=h, low=lo, close=c, volume=v)


def _sig(**kw) -> SignalObject:
    base = dict(timestamp=0, symbol="BTCUSDT", price=100.0, volume=100.0,
                avg_volume=100.0)
    base.update(kw)
    return SignalObject(**base)


def _bouncing(levels_low: float, levels_high: float, cycles: int = 4) -> list[Candle]:
    """Price repeatedly rejecting the same floor and ceiling."""
    out, i = [], 0
    mid = (levels_low + levels_high) / 2
    for _ in range(cycles):
        out.append(_c(i, mid, mid + 1, mid - 1, mid)); i += 1
        out.append(_c(i, mid, mid + 1, levels_low, mid, v=50.0)); i += 1   # tags low
        out.append(_c(i, mid, mid + 1, mid - 1, mid)); i += 1
        out.append(_c(i, mid, levels_high, mid - 1, mid, v=50.0)); i += 1  # tags high
    out.append(_c(i, mid, mid + 1, mid - 1, mid))
    return out


# -- Atlas: zones ----------------------------------------------------------- #
def test_repeated_touches_collapse_into_one_zone():
    """Five taps of the same shelf is one level, not five."""
    levels = atlas.find_levels(_bouncing(95.0, 105.0))
    assert len(levels.support) <= 2
    assert levels.support[0].touches >= 2


def test_a_multi_touch_zone_scores_higher_than_a_single_wick():
    candles = _bouncing(95.0, 105.0)
    candles.append(_c(len(candles), 100, 101, 80.0, 100))   # lone spike low
    candles += [_c(len(candles) + k, 100, 101, 99, 100) for k in range(4)]
    levels = atlas.find_levels(candles)
    shelf = max(levels.support, key=lambda z: z.touches)
    lone = min(levels.support, key=lambda z: z.touches)
    assert shelf.touches > lone.touches
    assert shelf.strength > lone.strength


def test_atlas_is_bullish_when_price_sits_on_support():
    candles = _bouncing(95.0, 105.0)
    candles.append(_c(len(candles), 96, 96.5, 95.0, 95.05))   # back at the floor
    f = atlas.observe(candles)
    assert f.verdict == BULLISH
    assert "support" in f.evidence


def test_atlas_is_bearish_at_resistance():
    candles = _bouncing(95.0, 105.0)
    candles.append(_c(len(candles), 104, 105.0, 104, 104.95))
    f = atlas.observe(candles)
    assert f.verdict == BEARISH


def test_atlas_is_neutral_with_no_history():
    assert atlas.observe([_c(0, 100, 101, 99, 100)]).verdict == NEUTRAL


def test_nearest_support_is_below_and_resistance_above():
    levels = atlas.find_levels(_bouncing(95.0, 105.0))
    sup = levels.nearest_support(100.0)
    res = levels.nearest_resistance(100.0)
    if sup:
        assert sup.center <= 100.0
    if res:
        assert res.center >= 100.0


# -- Echo: patterns in context ---------------------------------------------- #
def test_a_hammer_at_support_beats_the_same_hammer_mid_range():
    """The whole point of Echo: location changes what a shape means."""
    base = _bouncing(95.0, 105.0)
    levels = atlas.find_levels(base)

    at_support = base + [_c(len(base), 95.4, 95.5, 95.0, 95.45)]
    mid_range = base + [_c(len(base), 100.4, 100.5, 100.0, 100.45)]

    strong = echo.observe(at_support, levels)
    weak = echo.observe(mid_range, levels)
    assert strong.verdict == BULLISH and weak.verdict == BULLISH
    assert strong.confidence > weak.confidence


def test_echo_is_neutral_without_a_directional_pattern():
    # Balanced wicks, ordinary body: no hammer, star, doji or engulfing.
    candles = [_c(i, 100, 100.5, 99.5, 100.3) for i in range(5)]
    assert echo.observe(candles).verdict == NEUTRAL


def test_echo_reports_the_pattern_names():
    candles = [_c(0, 110, 111, 99, 100), _c(1, 99, 100, 98, 99.5),
               _c(2, 100, 108, 99, 107)]
    f = echo.observe(candles)
    assert "morning_star" in f.data["patterns"]


# -- Flow: order flow, with the measured inversion -------------------------- #
def test_flow_reads_buyer_dominance_as_mean_reverting():
    """Measured: positive delta preceded negative returns 8/8 sub-periods."""
    assert flow.DELTA_IS_MOMENTUM is False
    f = flow.observe(_sig(cvd_1m=50.0, volume=100.0))
    assert f.verdict == BEARISH
    assert "mean-reverting" in f.evidence


def test_flow_reads_seller_dominance_as_bullish():
    assert flow.observe(_sig(cvd_1m=-50.0, volume=100.0)).verdict == BULLISH


def test_small_delta_is_noise_not_a_signal():
    assert flow.observe(_sig(cvd_1m=1.0, volume=100.0)).verdict == NEUTRAL


def test_absorption_outranks_raw_delta():
    """Absorption is OHLCV-derived and better behaved, so it speaks first."""
    f = flow.observe(_sig(cvd_1m=50.0, volume=100.0,
                          absorption=Absorption(True, "bullish", 99.0, 0.9)))
    assert f.verdict == BULLISH          # not the BEARISH delta read
    assert f.confidence > flow.DELTA_MAX_CONFIDENCE


# -- Sentinel --------------------------------------------------------------- #
def _risk() -> RiskManager:
    return RiskManager(Settings(mode=RunMode.SIM, start_equity=1000), equity=1000)


def test_sentinel_is_clear_on_a_fresh_account():
    f = sentinel.observe(_risk(), 0)
    assert f.verdict == NEUTRAL
    assert f.data["trades_remaining"] == RiskLimits.MAX_TRADES_PER_DAY


def test_sentinel_blocks_when_the_daily_cap_is_spent():
    rm = _risk()
    rm.check_kill_switch(0)
    for _ in range(RiskLimits.MAX_TRADES_PER_DAY):
        rm.on_trade_opened()
        rm.on_trade_closed(+1.0, 0)
    f = sentinel.observe(rm, 0)
    assert f.verdict == BLOCK
    assert "daily trade cap" in f.evidence


def test_sentinel_blocks_when_halted():
    rm = _risk()
    rm.manual_halt()
    assert sentinel.observe(rm, 0).verdict == BLOCK


# -- Maya: the conference --------------------------------------------------- #
def test_sentinel_block_overrides_every_bullish_opinion():
    rm = _risk()
    rm.manual_halt()
    candles = _bouncing(95.0, 105.0) + [_c(99, 95.4, 95.5, 95.0, 95.45)]
    conf = desk.confer(candles, _sig(cvd_1m=-50.0, volume=100.0), rm, now_ts=0)
    assert conf.blocked_by == "Sentinel"
    assert conf.agrees_with is None


def test_a_split_desk_stands_down_rather_than_averaging():
    """Two analysts pointing opposite ways is a reason not to act, not a
    reason to take a small position."""
    findings = [Finding("Atlas", BULLISH, 0.8), Finding("Echo", BEARISH, 0.8)]
    bull = sum(f.confidence * desk.WEIGHTS[f.agent] for f in findings if f.verdict == BULLISH)
    bear = sum(f.confidence * desk.WEIGHTS[f.agent] for f in findings if f.verdict == BEARISH)
    assert min(bull, bear) / (bull + bear) > desk.MAX_DISSENT


def test_agreement_produces_a_directional_call():
    candles = _bouncing(95.0, 105.0) + [_c(99, 95.4, 95.5, 95.0, 95.45)]
    # At support (Atlas bullish), hammer at support (Echo bullish),
    # sellers aggressive (Flow bullish under the measured inversion).
    conf = desk.confer(candles, _sig(cvd_1m=-50.0, volume=100.0, price=95.45))
    assert conf.verdict == BULLISH
    assert conf.supports("long")
    assert not conf.supports("short")


def test_weak_conviction_stays_neutral():
    candles = [_c(i, 100, 100.4, 99.6, 100.2) for i in range(30)]
    conf = desk.confer(candles, _sig(cvd_1m=0.0, volume=100.0))
    assert conf.verdict == NEUTRAL
    assert conf.agrees_with is None


def test_conference_summary_names_every_speaker():
    candles = _bouncing(95.0, 105.0) + [_c(99, 95.4, 95.5, 95.0, 95.45)]
    conf = desk.confer(candles, _sig(cvd_1m=-50.0, volume=100.0))
    summary = conf.summary()
    for name in ("Atlas", "Echo", "Flow"):
        assert name in summary


def test_findings_are_retrievable_by_agent():
    candles = _bouncing(95.0, 105.0) + [_c(99, 95.4, 95.5, 95.0, 95.45)]
    conf = desk.confer(candles, _sig(cvd_1m=-50.0, volume=100.0))
    assert conf.by_agent("Atlas") is not None
    assert conf.by_agent("Nobody") is None


def test_signed_confidence_encodes_direction():
    assert Finding("X", BULLISH, 0.5).signed == 0.5
    assert Finding("X", BEARISH, 0.5).signed == -0.5
    assert Finding("X", NEUTRAL, 0.5).signed == 0.0


# -- roster ----------------------------------------------------------------- #
def test_roster_covers_every_graph_node():
    from maya.agents.roster import NODE_TO_NAME

    assert set(NODE_TO_NAME) == {"manage", "signal", "strategy", "risk", "execution"}
    assert NODE_TO_NAME["strategy"] == "Maya"
    assert NODE_TO_NAME["risk"] == "Sentinel"


def test_echo_prefers_the_zone_that_matches_its_read():
    """A wide bar can graze support and resistance at once. A bullish pattern
    that tagged support must not be judged against a resistance zone it merely
    brushed."""
    levels = atlas.Levels(
        support=[atlas.Zone(95.0, 94.8, 95.2, 4, 100.0, 0, "support", 0.4)],
        resistance=[atlas.Zone(105.0, 104.8, 105.2, 4, 100.0, 0, "resistance", 0.9)],
    )
    # Hammer whose range spans both zones; strongest touched zone is the
    # resistance one, but the pattern is bullish and did tag support.
    wide_hammer = _c(0, 104.0, 105.0, 94.9, 104.5)
    f = echo.observe([wide_hammer], levels)
    assert f.verdict == BULLISH
    assert f.data["zone"] == "support"
