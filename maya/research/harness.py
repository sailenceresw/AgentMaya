"""The research harness: walk-forward test, pass gate and one-shot holdout.

A hypothesis is a function that turns candles into hypothetical trades
(``Sample``). The harness charges costs, splits the research period into
eight sub-periods, and applies a pass rule that is fixed here in code before
any result exists:

1. at least ``MIN_TRADES`` trades,
2. mean net return above zero,
3. t-stat at or above the Bonferroni-corrected threshold,
4. a positive mean in at least ``MIN_SIGN_PERIODS`` of ``N_SUBPERIODS``.

The last ``HOLDOUT_DAYS`` of data are frozen. ``ResearchData.research`` never
contains them, and ``run_holdout`` is the only function that reads them. It
runs once per hypothesis id, and only after a research pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from maya.data.models import Candle
from maya.research.costs import PRESETS, CostModel, Sample, SampleList
from maya.research.registry import HypothesisRegistry
from maya.research.stats import BASE_T_THRESHOLD, corrected_threshold, mean, t_stat

HOLDOUT_DAYS = 120
N_SUBPERIODS = 8
MIN_SIGN_PERIODS = 6
MIN_TRADES = 100
# The holdout is 120 days, far shorter than the research window, so it gets a
# smaller trade floor. It is a confirmation, not a second search: the sign
# must hold after costs with the uncorrected t of 2.0. This rule is an
# assumption made when the harness was written, not something from the spec.
MIN_HOLDOUT_TRADES = 30

_DAY_MS = 86_400_000

Generator = Callable[[dict[str, list[Candle]]], list[Sample]]


@dataclass(frozen=True)
class Hypothesis:
    """One pre-declared configuration. No parameters to tune after the fact."""

    id: str
    description: str
    generate: Generator
    cost: str = "taker"
    # Bar size the generator expects. The CLI loads this unless told otherwise.
    timeframe: str = "15m"
    # Set when the hypothesis is declared but cannot be tested yet, with the
    # reason. A blocked hypothesis is refused before anything runs, so it
    # writes no registry row and does not raise the threshold.
    blocked: str = ""

    @property
    def cost_model(self) -> CostModel:
        return PRESETS[self.cost]


class ResearchData:
    """Candles per symbol with the holdout fenced off.

    The cutoff is taken from the newest candle across all symbols, so every
    symbol shares one holdout window.
    """

    def __init__(self, candles: dict[str, list[Candle]],
                 holdout_days: int = HOLDOUT_DAYS) -> None:
        if not candles or not any(candles.values()):
            raise ValueError("no candles to research on")
        self._all = {s: sorted(c, key=lambda x: x.timestamp) for s, c in candles.items() if c}
        newest = max(c[-1].timestamp for c in self._all.values())
        self.start = min(c[0].timestamp for c in self._all.values())
        self.cutoff = newest - holdout_days * _DAY_MS
        self.end = newest
        if self.cutoff <= self.start:
            raise ValueError(
                f"data spans less than the {holdout_days}-day holdout; nothing left to research on")

    @property
    def research(self) -> dict[str, list[Candle]]:
        return {s: [c for c in cs if c.timestamp < self.cutoff] for s, cs in self._all.items()}

    def _everything(self) -> dict[str, list[Candle]]:
        """Holdout included. Called by ``run_holdout`` and nothing else."""
        return self._all


@dataclass
class Report:
    hypothesis_id: str
    kind: str
    cost: str
    round_trip_bps: float
    trades: int = 0
    missed: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    t_stat: float = 0.0
    threshold: float = BASE_T_THRESHOLD
    mean_gross_bps: float = 0.0
    mean_net_bps: float = 0.0
    total_net_pct: float = 0.0
    subperiod_net_bps: list[float | None] = field(default_factory=list)
    sign_share: float = 0.0
    passed: bool = False
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    def format(self) -> str:
        subs = "  ".join("   n/a" if v is None else f"{v:+6.2f}" for v in self.subperiod_net_bps)
        lines = [
            f"hypothesis      {self.hypothesis_id} ({self.kind})",
            f"cost model      {self.cost}, round trip {self.round_trip_bps:.1f} bps",
            f"trades          {self.trades}  (missed fills: {self.missed})",
            f"win rate        {self.win_rate:.1f}%",
            f"profit factor   {self.profit_factor:.2f}",
            f"mean gross      {self.mean_gross_bps:+.2f} bps per trade",
            f"mean net        {self.mean_net_bps:+.2f} bps per trade",
            f"total net       {self.total_net_pct:+.2f}% (sum of trade returns, unlevered)",
            f"t-stat          {self.t_stat:+.2f}  (needs >= {self.threshold:.2f})",
        ]
        if self.subperiod_net_bps:
            lines += [f"sub-periods     {subs}  (mean net bps)",
                      f"sign share      {self.sign_share:.0%} of sub-periods positive"]
        lines += [f"note            {n}" for n in self.notes]
        lines.append(f"result          {'PASS' if self.passed else 'FAIL'}: {self.reason}")
        return "\n".join(lines)


def _base_report(hyp: Hypothesis, kind: str, samples: list[Sample],
                 seed: int) -> tuple[Report, list[Sample], list[float]]:
    cost = hyp.cost_model
    filled, missed = cost.fill(samples, seed=seed)
    net = [cost.net(s.gross_return) for s in filled]
    rep = Report(hyp.id, kind, cost.name, cost.round_trip_bps,
                 trades=len(filled), missed=missed,
                 notes=list(getattr(samples, "notes", [])))
    if net:
        wins = [r for r in net if r > 0]
        losses = [r for r in net if r < 0]
        rep.win_rate = len(wins) / len(net) * 100
        rep.profit_factor = (sum(wins) / -sum(losses)) if losses else float("inf")
        rep.t_stat = t_stat(net)
        rep.mean_gross_bps = mean([s.gross_return for s in filled]) * 10_000
        rep.mean_net_bps = mean(net) * 10_000
        rep.total_net_pct = sum(net) * 100
    return rep, filled, net


def evaluate(hyp: Hypothesis, samples: list[Sample], start: int, end: int,
             n_tests: int, seed: int = 7) -> Report:
    """Walk-forward evaluation with the fixed pass rule. Pure: no I/O."""
    rep, filled, net = _base_report(hyp, "research", samples, seed)
    rep.threshold = corrected_threshold(n_tests)

    span = max(end - start, 1)
    buckets: list[list[float]] = [[] for _ in range(N_SUBPERIODS)]
    for s, r in zip(filled, net, strict=True):
        idx = min(int((s.timestamp - start) * N_SUBPERIODS / span), N_SUBPERIODS - 1)
        buckets[max(idx, 0)].append(r)
    rep.subperiod_net_bps = [mean(b) * 10_000 if b else None for b in buckets]
    # An empty sub-period does not count as agreeing: no trades is no evidence.
    positive = sum(1 for v in rep.subperiod_net_bps if v is not None and v > 0)
    rep.sign_share = positive / N_SUBPERIODS

    failures = []
    if rep.trades < MIN_TRADES:
        failures.append(f"only {rep.trades} trades, needs {MIN_TRADES}")
    if rep.mean_net_bps <= 0:
        failures.append(f"mean net {rep.mean_net_bps:+.2f} bps is not above zero after costs")
    if rep.t_stat < rep.threshold:
        failures.append(f"t-stat {rep.t_stat:+.2f} below corrected threshold "
                        f"{rep.threshold:.2f} ({n_tests} tests)")
    if positive < MIN_SIGN_PERIODS:
        failures.append(f"positive in {positive} of {N_SUBPERIODS} sub-periods, "
                        f"needs {MIN_SIGN_PERIODS}")
    rep.passed = not failures
    rep.reason = "all four gates cleared" if rep.passed else "; ".join(failures)
    return rep


def run_research(hyp: Hypothesis, data: ResearchData,
                 registry: HypothesisRegistry) -> Report:
    """Test ``hyp`` on the research window and write a registry row.

    The row is written whether the test passes, fails or raises. A crash that
    left no trace would be a test that never counted towards the threshold.
    """
    # This test counts towards its own threshold.
    n_tests = registry.count_tests() + 1
    try:
        samples = hyp.generate(data.research)
        rep = evaluate(hyp, samples, data.start, data.cutoff, n_tests)
    except Exception as exc:
        registry.record(hyp.id, hyp.description, None, False,
                        f"crashed: {type(exc).__name__}: {exc}"[:300])
        raise
    registry.record(hyp.id, hyp.description, rep.t_stat, rep.passed, rep.reason)
    return rep


def run_holdout(hyp: Hypothesis, data: ResearchData,
                registry: HypothesisRegistry) -> Report:
    """Evaluate ``hyp`` on the frozen holdout. Once per hypothesis id, ever.

    Refuses unless the hypothesis already passed research: the holdout is a
    confirmation, and spending it on an idea that failed research would waste
    the only untouched data there is.
    """
    if not registry.has_research_pass(hyp.id):
        raise PermissionError(
            f"{hyp.id!r} has no passing research run; the holdout stays closed")
    registry.claim_holdout(hyp.id)  # raises HoldoutAlreadyUsed on a second call
    try:
        # The generator sees earlier candles so indicators can warm up, but
        # only trades entered inside the holdout window are judged.
        generated = hyp.generate(data._everything())
        samples = SampleList((s for s in generated if s.timestamp >= data.cutoff),
                             getattr(generated, "notes", []))
        rep, _, _ = _base_report(hyp, "holdout", samples, seed=11)
    except Exception as exc:
        registry.record(hyp.id, hyp.description, None, False,
                        f"holdout crashed: {type(exc).__name__}: {exc}"[:300], kind="holdout")
        raise
    failures = []
    if rep.trades < MIN_HOLDOUT_TRADES:
        failures.append(f"only {rep.trades} trades, needs {MIN_HOLDOUT_TRADES}")
    if rep.mean_net_bps <= 0:
        failures.append(f"mean net {rep.mean_net_bps:+.2f} bps is not above zero after costs")
    if rep.t_stat < BASE_T_THRESHOLD:
        failures.append(f"t-stat {rep.t_stat:+.2f} below {BASE_T_THRESHOLD:.2f}")
    rep.passed = not failures
    rep.reason = "holdout confirmed" if rep.passed else "; ".join(failures)
    registry.record(hyp.id, hyp.description, rep.t_stat, rep.passed, rep.reason, kind="holdout")
    return rep
