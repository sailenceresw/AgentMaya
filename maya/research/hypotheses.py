"""Catalogue of pre-declared hypotheses.

Each entry is one fixed configuration. To try a different parameter, register
a new id; editing an entry after seeing its result is tuning, and the registry
row for the old result stays either way.
"""
from __future__ import annotations

from maya.data.models import Candle
from maya.research.costs import Sample
from maya.research.harness import Hypothesis
from maya.signals.indicators import delta_for


def forward_return(candles: list[Candle], i: int, horizon: int) -> float | None:
    """Close-to-close return from bar ``i`` to ``i + horizon``, or None.

    Entry is at the close of bar ``i``, the first moment a signal computed
    from that bar is known.
    """
    j = i + horizon
    if j >= len(candles) or candles[i].close <= 0:
        return None
    return candles[j].close / candles[i].close - 1.0


def _cvd_follow(data: dict[str, list[Candle]]) -> list[Sample]:
    """Trade with the bar's delta and hold one bar. This is the reading the
    legacy setups assume; the README measured it wrong-signed, so it is here
    as the reference a new idea has to beat, not as a candidate."""
    out: list[Sample] = []
    for candles in data.values():
        for i, c in enumerate(candles):
            d = delta_for(c)
            fwd = forward_return(candles, i, 1)
            if fwd is None or d == 0:
                continue
            out.append(Sample(c.timestamp, fwd if d > 0 else -fwd))
    return out


CATALOG: dict[str, Hypothesis] = {}


def register(hyp: Hypothesis) -> Hypothesis:
    if hyp.id in CATALOG:
        raise ValueError(f"hypothesis id {hyp.id!r} is already declared")
    CATALOG[hyp.id] = hyp
    return hyp


register(Hypothesis(
    id="cvd_follow_baseline",
    description="Reference: trade in the direction of each bar's delta, hold one bar, taker costs.",
    generate=_cvd_follow,
))
