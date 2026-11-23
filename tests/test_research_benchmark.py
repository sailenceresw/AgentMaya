"""Gate 5: a pass must beat simply holding the coin.

Regression for 2026-11-19: trend_30d_hold_7d_2020_2024 passed gates 1-4 while
always-long on the same bars did better, because the coins rose.
"""
from __future__ import annotations

import math
import random

import pytest

from maya.data.models import Candle
from maya.research.costs import Sample
from maya.research.harness import (
    Hypothesis,
    ResearchData,
    drift_per_ms,
    evaluate,
    run_holdout,
    run_research,
)
from maya.research.registry import HypothesisRegistry

DAY = 86_400_000
STEP = DAY // 4
G = 0.002          # invented drift: 20 bps a bar, so holding beats the 10 bps round trip


def _rising(days: int = 400, seed: int = 5) -> list[Candle]:
    """Invented series that rises 20 bps a bar with independent noise."""
    rng = random.Random(seed)
    out = []
    for i in range(days * 4):
        c = 100 * math.exp(G * i + rng.gauss(0, 0.005))
        out.append(Candle(timestamp=i * STEP, open=c, high=c, low=c, close=c,
                          volume=10, taker_buy_volume=5))
    return out


@pytest.fixture
def data():
    return ResearchData({"UPUSDT": _rising()})


@pytest.fixture
def registry(tmp_path):
    return HypothesisRegistry(str(tmp_path / "j.db"))


def _always_long(d):
    out = []
    for sym, cs in d.items():
        for i in range(len(cs) - 1):
            out.append(Sample(cs[i].timestamp, cs[i + 1].close / cs[i].close - 1, sym, 1, STEP))
    return out


def _peeks(d):
    """Cheats by reading the next bar's noise: a real edge on top of drift.
    Only here to show the gate lets a true edge through."""
    out = []
    for sym, cs in d.items():
        for i in range(len(cs) - 1):
            fwd = cs[i + 1].close / cs[i].close - 1
            side = 1 if fwd > G else -1
            out.append(Sample(cs[i].timestamp, side * fwd, sym, side, STEP))
    return out


def _bars(closes):
    return [Candle(timestamp=i * 1000, open=1, high=1, low=1, close=c, volume=1)
            for i, c in enumerate(closes)]


def test_drift_is_the_mean_simple_return_per_ms():
    assert drift_per_ms({"X": _bars([100, 200])})["X"] == pytest.approx(math.log(2) / 1000)
    assert drift_per_ms({"Y": []}) == {}


def test_drift_counts_what_holding_a_volatile_coin_earns_per_trade():
    # Ends where it started, so log growth is zero, but a long held one bar at a
    # time averages +25% ((+100% - 50%) / 2). A zero hurdle would hand that
    # to any always-long rule as if it were skill.
    assert drift_per_ms({"Z": _bars([100, 200, 100])})["Z"] == \
        pytest.approx(math.log1p(0.25) / 1000)


def test_holding_a_rising_coin_passes_four_gates_but_not_five(data, registry):
    hyp = Hypothesis("long_only", "always long", _always_long)
    research = data.research
    four = evaluate(hyp, hyp.generate(research), data.start, data.cutoff, n_tests=1)
    assert four.passed, four.reason                  # the old harness called this a pass
    rep = run_research(hyp, data, registry)
    assert not rep.passed and "does not beat buy-and-hold" in rep.reason
    assert rep.excess_bps < 0 < rep.mean_net_bps
    assert "vs buy-and-hold" in rep.format()


def test_a_real_edge_on_top_of_drift_passes(data, registry):
    rep = run_research(Hypothesis("peeks", "", _peeks), data, registry)
    assert rep.passed, rep.reason
    assert rep.excess_bps > 0 and rep.reason == "all five gates cleared"


def test_shorts_get_credit_for_losing_less_than_the_drift(data):
    # Short every bar on a rising coin: loses about the drift, so its excess
    # over buy-and-hold is about zero before costs, never a fake gain.
    def short(d):
        return [Sample(s.timestamp, -s.gross_return, s.symbol, -1, s.hold_ms)
                for s in _always_long(d)]
    research = data.research
    rep = evaluate(Hypothesis("short", "", short), short(research), data.start, data.cutoff,
                   n_tests=1, drift=drift_per_ms(research))
    assert rep.mean_net_bps < -20
    assert rep.excess_bps == pytest.approx(-10.0, abs=2.0)    # just the round trip


def test_samples_without_direction_cannot_pass(data, registry):
    def bare(d):
        return [Sample(s.timestamp, s.gross_return) for s in _peeks(d)]
    rep = run_research(Hypothesis("bare", "", bare), data, registry)
    assert not rep.passed and "cannot check against buy-and-hold" in rep.reason


def test_holdout_also_has_to_beat_holding(data, registry):
    # Peeks before the cutoff (passes research), plain always-long after it.
    def gen(d):
        early = [s for s in _peeks(d) if s.timestamp < data.cutoff]
        late = [s for s in _always_long(d) if s.timestamp >= data.cutoff]
        return early + late
    hyp = Hypothesis("goes_long", "", gen)
    assert run_research(hyp, data, registry).passed
    rep = run_holdout(hyp, data, registry)
    assert rep.mean_net_bps > 0 and not rep.passed
    assert "does not beat buy-and-hold" in rep.reason
