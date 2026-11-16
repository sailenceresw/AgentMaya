"""Research harness: the guarantees that keep a pass honest."""
from __future__ import annotations

import random

import pytest

from maya.data.models import Candle
from maya.research import __main__ as cli
from maya.research.costs import MAKER, TAKER, Sample, custom
from maya.research.harness import (
    HOLDOUT_DAYS,
    Hypothesis,
    ResearchData,
    evaluate,
    run_holdout,
    run_research,
)
from maya.research.registry import HoldoutAlreadyUsed, HypothesisRegistry
from maya.research.stats import corrected_threshold, t_stat

DAY = 86_400_000


def _candles(days: int = 400, per_day: int = 4) -> list[Candle]:
    """Invented flat series; only the timestamps matter to these tests."""
    step = DAY // per_day
    return [Candle(timestamp=i * step, open=100, high=101, low=99, close=100,
                   volume=10, taker_buy_volume=5) for i in range(days * per_day)]


def _edge(mean_bps: float, sd_bps: float = 30.0):
    """A generator with a known true edge, one trade per candle."""
    def gen(data):
        rng = random.Random(1)
        return [Sample(c.timestamp, rng.gauss(mean_bps, sd_bps) / 10_000)
                for cs in data.values() for c in cs]
    return gen


@pytest.fixture
def registry(tmp_path):
    return HypothesisRegistry(str(tmp_path / "j.db"))


@pytest.fixture
def data():
    return ResearchData({"BTCUSDT": _candles()})


def test_costs_reduce_returns():
    assert TAKER.round_trip_bps == 10.0
    assert TAKER.net(0.002) == pytest.approx(0.001)
    # An idea that makes 5 bps gross loses 5 bps after a 10 bps round trip.
    assert TAKER.net(0.0005) < 0


def test_maker_preset_is_cheaper_but_misses_fills():
    samples = [Sample(i, 0.001) for i in range(1000)]
    filled, missed = MAKER.fill(samples)
    assert MAKER.round_trip_bps < TAKER.round_trip_bps
    assert missed > 0 and len(filled) + missed == 1000
    assert 500 < len(filled) < 700          # around the 60% fill probability
    assert MAKER.fill(samples) == (filled, missed)   # seeded, so repeatable


def test_cost_model_rejects_nonsense():
    with pytest.raises(ValueError):
        custom(1, 1, fill_probability=0)
    with pytest.raises(ValueError):
        custom(-1, 0)


def test_threshold_grows_as_registry_grows():
    assert corrected_threshold(1) == 2.0     # never softer than the old bar
    assert corrected_threshold(10) > corrected_threshold(2) > 2.0
    assert corrected_threshold(10) == pytest.approx(2.807, abs=0.001)


def test_t_stat_undefined_cases_are_zero():
    assert t_stat([]) == 0.0
    assert t_stat([1.0]) == 0.0
    assert t_stat([1.0, 1.0, 1.0]) == 0.0


def test_research_never_sees_holdout(data):
    seen = [c.timestamp for cs in data.research.values() for c in cs]
    assert max(seen) < data.cutoff
    assert data.end - data.cutoff == HOLDOUT_DAYS * DAY


def test_too_little_data_for_a_holdout_is_refused():
    with pytest.raises(ValueError):
        ResearchData({"BTCUSDT": _candles(days=90)})


def test_real_edge_passes_and_writes_a_row(data, registry):
    hyp = Hypothesis("strong", "invented 30 bps edge", _edge(30.0))
    rep = run_research(hyp, data, registry)
    assert rep.passed, rep.reason
    assert rep.sign_share >= 0.75 and len(rep.subperiod_net_bps) == 8
    row = registry.rows()[0]
    assert (row.id, row.passed_gate, row.kind) == ("strong", True, "research")


def test_edge_smaller_than_costs_fails_after_costs(data, registry):
    # 5 bps gross is a real edge, but under the 10 bps round trip.
    rep = run_research(Hypothesis("thin", "invented 5 bps edge", _edge(5.0)), data, registry)
    assert not rep.passed
    assert rep.mean_gross_bps > 0 > rep.mean_net_bps
    assert registry.rows()[0].passed_gate is False


def test_too_few_trades_fails_even_when_profitable(data):
    hyp = Hypothesis("few", "", lambda d: [])
    samples = [Sample(data.start + i * DAY, 0.01) for i in range(50)]
    rep = evaluate(hyp, samples, data.start, data.cutoff, n_tests=1)
    assert not rep.passed and "only 50 trades" in rep.reason


def test_edge_in_one_subperiod_only_fails_the_sign_rule(data):
    hyp = Hypothesis("lumpy", "", lambda d: [])
    span = data.cutoff - data.start
    rng = random.Random(3)
    samples = [Sample(data.start + int(span * 0.01 * (i % 10)), 0.01 + rng.gauss(0, 0.001))
               for i in range(400)]                          # all inside the first eighth
    rep = evaluate(hyp, samples, data.start, data.cutoff, n_tests=1)
    assert not rep.passed and "sub-periods" in rep.reason


def test_crashed_test_still_writes_a_row(data, registry):
    def boom(_):
        raise RuntimeError("bad indicator")
    with pytest.raises(RuntimeError):
        run_research(Hypothesis("broken", "crashes", boom), data, registry)
    row = registry.rows()[0]
    assert row.id == "broken" and not row.passed_gate and "crashed" in row.reason
    assert registry.count_tests() == 1       # and it raises the next threshold


def test_each_test_raises_the_bar_for_the_next(data, registry):
    hyp = Hypothesis("a", "", _edge(30.0))
    first = run_research(hyp, data, registry)
    second = run_research(Hypothesis("b", "", _edge(30.0)), data, registry)
    assert second.threshold > first.threshold


def test_holdout_needs_a_research_pass_first(data, registry):
    hyp = Hypothesis("thin", "", _edge(5.0))
    run_research(hyp, data, registry)
    with pytest.raises(PermissionError):
        run_holdout(hyp, data, registry)
    assert not registry.holdout_used("thin")     # a refusal does not spend it


def test_holdout_refuses_a_second_run(data, registry):
    hyp = Hypothesis("strong", "", _edge(30.0))
    run_research(hyp, data, registry)
    rep = run_holdout(hyp, data, registry)
    assert rep.kind == "holdout" and rep.passed, rep.reason
    with pytest.raises(HoldoutAlreadyUsed):
        run_holdout(hyp, data, registry)
    assert [r.kind for r in registry.rows()] == ["research", "holdout"]
    assert registry.count_tests() == 1           # holdout rows do not count as new tests


def test_holdout_is_spent_even_if_it_crashes(data, registry):
    calls = {"n": 0}

    def gen(d):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("crash on holdout")
        return _edge(30.0)(d)
    hyp = Hypothesis("flaky", "", gen)
    run_research(hyp, data, registry)
    with pytest.raises(RuntimeError):
        run_holdout(hyp, data, registry)
    with pytest.raises(HoldoutAlreadyUsed):
        run_holdout(hyp, data, registry)


def test_holdout_judges_only_holdout_trades(data, registry):
    # Strong edge before the cutoff, losing after it: research passes, holdout fails.
    def gen(d):
        return [Sample(c.timestamp, 0.003 if c.timestamp < data.cutoff else -0.003)
                for cs in d.values() for c in cs if (c.timestamp // 1000) % 7 != 0] + \
               [Sample(c.timestamp, 0.0029 if c.timestamp < data.cutoff else -0.0031)
                for cs in d.values() for c in cs if (c.timestamp // 1000) % 7 == 0]
    hyp = Hypothesis("decays", "", gen)
    assert run_research(hyp, data, registry).passed
    assert not run_holdout(hyp, data, registry).passed


def _rows(days=400, per_day=4):
    step = DAY // per_day
    return [[i * step, 100, 101, 99, 100 + (i % 3), 10, 4 + (i % 3)] for i in range(days * per_day)]


def test_cli_run_prints_report_and_records(tmp_path, capsys):
    db = str(tmp_path / "j.db")
    loader = lambda settings, start, end, cache_path=None: _rows()  # noqa: E731
    code = cli.main(["run", "cvd_follow_baseline", "--symbols", "BTCUSDT", "--db", db],
                    loader=loader)
    out = capsys.readouterr().out
    assert code in (0, 1) and "t-stat" in out and "sub-periods" in out
    assert cli.main(["registry", "--db", db]) == 0
    assert "cvd_follow_baseline" in capsys.readouterr().out


def test_cli_unknown_hypothesis_and_list(capsys):
    assert cli.main(["run", "nope"]) == 2
    assert cli.main(["list"]) == 0
    assert "cvd_follow_baseline" in capsys.readouterr().out


def test_cli_holdout_refused_without_a_pass(tmp_path, capsys):
    loader = lambda settings, start, end, cache_path=None: _rows()  # noqa: E731
    code = cli.main(["holdout", "cvd_follow_baseline", "--symbols", "BTCUSDT",
                     "--db", str(tmp_path / "j.db")], loader=loader)
    assert code == 3 and "refused" in capsys.readouterr().err
