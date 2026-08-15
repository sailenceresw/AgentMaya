"""Scripted candle scenarios for demos and integration tests.

These feeds replay hand-crafted, textbook setups so the *unmodified* strategy,
risk and execution layers can be exercised end-to-end (open -> manage -> close
-> journal). They prove the pipeline works on valid inputs; they are NOT a
market model. Real evaluation must use the ``paper``/``testnet`` feeds or a
historical backtest.

Enable with ``MAYA_SCENARIO=true`` (mode stays ``sim``).
"""
from __future__ import annotations

from typing import Iterator

from maya.config import Settings
from maya.data.models import Candle, Trade

_TF_MS = 60_000


def _trades(ts: int, lo: float, hi: float, buy_ratio: float, total: float) -> list[Trade]:
    """Build synthetic trades spanning [lo, hi] with a given aggressive-buy ratio."""
    n = 12
    trades: list[Trade] = []
    for k in range(n):
        px = lo + (hi - lo) * (k / (n - 1))
        qty = total / n
        is_buyer_maker = (k / n) >= buy_ratio  # first `buy_ratio` fraction are buys
        trades.append(Trade(ts, px, qty, is_buyer_maker))
    return trades


def _candle(ts: int, o: float, h: float, l: float, c: float,
            vol: float, buy_ratio: float) -> Candle:
    return Candle(ts, o, h, l, c, vol, _trades(ts, l, h, buy_ratio, vol))


class ScenarioFeed:
    """Replays a deterministic sequence containing a clean liquidity-sweep short.

    Structure: a sharp down-leg prints a distant swing low (far target), price
    retraces up, then a high-sweep candle pokes above the local swing high on
    heavy sell volume and closes back inside -> short toward the distant low,
    giving a >=1:5 reward:risk that satisfies the real risk gate.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.t0 = 1_700_000_000_000

    def _build(self) -> list[Candle]:
        cs: list[Candle] = []
        t = self.t0

        def step(o, h, l, c, vol, br):
            nonlocal t
            cs.append(_candle(t, o, h, l, c, vol, br))
            t += _TF_MS

        # 0) Two consolidation candles so the signal layer has a full lookback
        #    (>=10 candles) by the time the sweep prints — needed for the AMT
        #    phase to register 'expansion' and contribute its confluence point.
        step(64_100, 64_400, 64_000, 64_200, 140, 0.5)
        step(64_200, 64_400, 63_950, 64_150, 140, 0.5)

        # 1) Print a deep swing low (~63,700) — the distant target pool. Kept
        #    within the 15-candle lookback so it remains the active target.
        step(64_200, 64_300, 63_700, 63_800, 200, 0.30)

        # 2) Clean retrace up to a local swing high (~67,000), each candle a
        #    higher high so the local high is unambiguous.
        for o, h, l, c in (
            (63_800, 64_300, 63_750, 64_200),
            (64_200, 64_800, 64_150, 64_700),
            (64_700, 65_300, 64_650, 65_200),
            (65_200, 65_800, 65_150, 65_700),
            (65_700, 66_300, 65_650, 66_200),
            (66_200, 66_700, 66_150, 66_600),
            (66_600, 67_000, 66_550, 66_950),
        ):
            step(o, h, l, c, 150, 0.6)

        # 3) The sweep: poke above the ~67,000 swing high on heavy SELL volume
        #    and close back inside (~66,800). cvd_1m < 0 => buyers absorbed.
        #    entry 66,800 / SL ~67,390 / target swing-low 63,700 => R:R ~5.3.
        step(o=66_950, h=67_120, l=66_750, c=66_800, vol=520, br=0.20)

        # 4) Follow-through down so the take-profit resolves during management.
        for o, h, l, c in (
            (66_800, 66_850, 66_300, 66_400),
            (66_400, 66_450, 65_700, 65_800),
            (65_800, 65_850, 65_000, 65_100),
            (65_100, 65_150, 64_300, 64_400),
            (64_400, 64_450, 63_600, 63_650),
        ):
            step(o, h, l, c, 170, 0.35)

        return cs

    def candles(self) -> Iterator[Candle]:
        yield from self._build()
