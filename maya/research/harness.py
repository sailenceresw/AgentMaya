"""The research harness: walk-forward test, pass gate and one-shot holdout.

A hypothesis is a function that turns candles into hypothetical trades
(``Sample``). The harness charges costs, splits the research period into
eight sub-periods, and applies a pass rule that is fixed here in code before
any result exists:

1. at least ``MIN_TRADES`` trades,
2. mean net return above zero,
3. t-stat at or above the Bonferroni-corrected threshold,
4. a positive mean in at least ``MIN_SIGN_PERIODS`` of ``N_SUBPERIODS``,
5. it beats buy-and-hold: after taking out what holding the coin earned over
   each trade's hold (``direction * drift``), the mean is still above zero
   and its t-stat clears the same corrected threshold.

Gate 5 was added on 2026-11-19, after trend_30d_hold_7d_2020_2024 passed
gates 1-4 while always-long on the same bars did better (+215 vs +202 bps):
a long-biased rule on coins that rose can pass without predicting anything.
The drift is the coin's mean simple bar return over the window being judged
(see drift_per_ms), so it is a hurdle, not a signal, and may use the whole
window. A rule that is long and short about equally is barely affected; a
rule that is mostly long has to beat holding.

The last ``HOLDOUT_DAYS`` of data are frozen. ``ResearchData.research`` never
contains them, and ``run_holdout`` is the only function that reads them. It
runs once per hypothesis id, and only after a research pass.
"""
from __future__ import annotations

import math
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


def drift_per_ms(candles: dict[str, list[Candle]]) -> dict[str, float]:
    """Each coin's buy-and-hold rate per millisecond: ``log1p`` of the mean
    simple bar return, over the bar length.

    The mean of simple returns, not first-to-last log growth: the harness
    judges the arithmetic mean per trade, and on a volatile coin that is well
    above the log growth (by about half the variance), so a log hurdle would
    let plain long exposure through.
    """
    out: dict[str, float] = {}
    for sym, cs in candles.items():
        rets = [b.close / a.close - 1 for a, b in zip(cs, cs[1:], strict=False)
                if a.close > 0 and b.timestamp > a.timestamp]
        if not rets:
            continue
        bar_ms = sorted(b.timestamp - a.timestamp for a, b in zip(cs, cs[1:], strict=False)
                        )[(len(cs) - 1) // 2]
        mu = sum(rets) / len(rets)
        if bar_ms > 0 and mu > -1:
            out[sym] = math.log1p(mu) / bar_ms
    return out


def excess_returns(filled: list[Sample], net: list[float],
                   drift: dict[str, float]) -> list[float] | None:
    """Net return minus what the same direction earned from drift alone, per
    trade; None if any trade lacks the coin, direction or hold to check it."""
    out = []
    for s, r in zip(filled, net, strict=True):
        if s.direction not in (1, -1) or s.hold_ms <= 0 or s.symbol not in drift:
            return None
        out.append(r - s.direction * math.expm1(drift[s.symbol] * s.hold_ms))
    return out


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
    # Comma-separated symbols the test is declared on; empty means the CLI
    # default. Part of the declaration, like the timeframe.
    symbols: str = ""
    # Data window the test is declared on (ISO dates); empty means the CLI
    # default. The last HOLDOUT_DAYS of it are the holdout, as always.
    start: str = ""
    end: str = ""
    # Set when the hypothesis is declared but cannot be tested yet, with the
    # reason. A blocked hypothesis is refused before anything runs, so it
    # writes no registry row and does not raise the threshold.
    blocked: str = ""
    # Optional check run just before a test: returns why the hypothesis cannot
    # run in this environment (a missing model, say), or '' if it can. Like
    # ``blocked``, a refusal here writes no row.
    precheck: Callable[[], str] | None = None

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
    # Gate 5: mean net minus buy-and-hold drift, and its t. None = not checked.
    excess_bps: float | None = None
    excess_t: float | None = None
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
        if self.excess_bps is not None:
            lines.append(f"vs buy-and-hold {self.excess_bps:+.2f} bps per trade after taking out "
                         f"the coin's drift, t {self.excess_t:+.2f}")
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


def _check_excess(rep: Report, filled: list[Sample], net: list[float],
                  drift: dict[str, float]) -> bool:
    """Fill the gate-5 fields; False if the samples cannot be checked."""
    excess = excess_returns(filled, net, drift)
    if excess is None:
        return False
    if excess:
        rep.excess_bps = mean(excess) * 10_000
        rep.excess_t = t_stat(excess)
    return True


def evaluate(hyp: Hypothesis, samples: list[Sample], start: int, end: int,
             n_tests: int, seed: int = 7,
             drift: dict[str, float] | None = None) -> Report:
    """Walk-forward evaluation with the fixed pass rule. Pure: no I/O.

    ``drift`` (from :func:`drift_per_ms`) turns on gate 5. run_research always
    passes it; only unit tests of gates 1-4 leave it out.
    """
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
    gates = "four"
    if drift is not None:
        gates = "five"
        if not _check_excess(rep, filled, net, drift):
            failures.append("cannot check against buy-and-hold: trades carry no coin, "
                            "direction or hold time")
        elif rep.excess_bps is not None and (rep.excess_bps <= 0
                                             or rep.excess_t < rep.threshold):
            failures.append(f"does not beat buy-and-hold: {rep.excess_bps:+.2f} bps after "
                            f"the coin's drift, t {rep.excess_t:+.2f} (needs >= "
                            f"{rep.threshold:.2f})")
    rep.passed = not failures
    rep.reason = f"all {gates} gates cleared" if rep.passed else "; ".join(failures)
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
        research = data.research
        samples = hyp.generate(research)
        rep = evaluate(hyp, samples, data.start, data.cutoff, n_tests,
                       drift=drift_per_ms(research))
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
        everything = data._everything()
        generated = hyp.generate(everything)
        samples = SampleList((s for s in generated if s.timestamp >= data.cutoff),
                             getattr(generated, "notes", []))
        rep, filled, net = _base_report(hyp, "holdout", samples, seed=11)
        # The hurdle is what holding earned during the holdout itself.
        checkable = _check_excess(rep, filled, net, drift_per_ms(
            {s: [c for c in cs if c.timestamp >= data.cutoff] for s, cs in everything.items()}))
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
    if not checkable:
        failures.append("cannot check against buy-and-hold: trades carry no coin, "
                        "direction or hold time")
    elif rep.excess_bps is not None and rep.excess_bps <= 0:
        failures.append(f"does not beat buy-and-hold: {rep.excess_bps:+.2f} bps after "
                        "the coin's drift")
    rep.passed = not failures
    rep.reason = "holdout confirmed" if rep.passed else "; ".join(failures)
    registry.record(hyp.id, hyp.description, rep.t_stat, rep.passed, rep.reason, kind="holdout")
    return rep
