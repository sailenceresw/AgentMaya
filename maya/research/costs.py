"""Cost models for the research harness.

Every return the harness judges is charged a round trip before it counts.
The README measured the order-flow effect at about 1 bps against about 10 bps
of taker cost, so a harness that judged gross returns would pass exactly the
ideas that lose money once traded.

Two presets:

- ``TAKER``: 4 bps fee plus 1 bp slippage per side, 10 bps round trip. This is
  the default and matches the README's working number.
- ``MAKER``: post-only entries. Lower fee and no slippage, but a resting order
  only fills some of the time. The haircut drops trades deterministically
  (seeded), so a missed trade is a trade that never happened rather than a
  smaller one. The 2 bps fee and 60% fill rate are assumptions, not
  measurements; Phase 3 replaces the fill rate with a simulated order book.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    """One hypothetical trade: when it was entered and its gross return.

    ``gross_return`` is a fraction (0.001 = 10 bps) already signed in the
    direction the hypothesis traded, so a short that worked is positive.
    """

    timestamp: int
    gross_return: float


@dataclass(frozen=True)
class CostModel:
    name: str
    fee_bps_per_side: float
    slippage_bps_per_side: float
    fill_probability: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 < self.fill_probability <= 1.0:
            raise ValueError(f"fill_probability must be in (0, 1], got {self.fill_probability}")
        if self.fee_bps_per_side < 0 or self.slippage_bps_per_side < 0:
            raise ValueError("fees and slippage cannot be negative")

    @property
    def round_trip_bps(self) -> float:
        return 2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side)

    def net(self, gross_return: float) -> float:
        return gross_return - self.round_trip_bps / 10_000.0

    def fill(self, samples: list[Sample], seed: int = 7) -> tuple[list[Sample], int]:
        """Return the samples that filled and how many were missed.

        The draw is seeded so a rerun of the same hypothesis on the same data
        gets the same fills; otherwise a maker test could be rerun until the
        lucky subset turned up.
        """
        if self.fill_probability >= 1.0:
            return list(samples), 0
        rng = random.Random(seed)
        filled = [s for s in samples if rng.random() < self.fill_probability]
        return filled, len(samples) - len(filled)


TAKER = CostModel("taker", fee_bps_per_side=4.0, slippage_bps_per_side=1.0)
MAKER = CostModel("maker", fee_bps_per_side=2.0, slippage_bps_per_side=0.0,
                  fill_probability=0.6)

PRESETS: dict[str, CostModel] = {m.name: m for m in (TAKER, MAKER)}


def custom(fee_bps_per_side: float, slippage_bps_per_side: float,
           fill_probability: float = 1.0) -> CostModel:
    return CostModel("custom", fee_bps_per_side, slippage_bps_per_side, fill_probability)
