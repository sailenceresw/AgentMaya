"""Layer 2 orchestrator — turns a stream of candles into SignalObjects.

Maintains the rolling state (candle window, CVD series, price series) needed by
the stateless indicator functions and emits one :class:`SignalObject` per
candle close.
"""
from __future__ import annotations

from collections import deque

import numpy as np

from maya.data.models import Candle
from maya.signals import indicators as ind
from maya.signals import oscillators as osc
from maya.signals import patterns as pat
from maya.signals import technicals as ta
from maya.signals.models import HTFContext, SignalObject
from maya.signals.timeframes import TimeframeAggregator, parse_timeframe

WINDOW = 60  # candles retained for profile / phase calculations
# Spec 6.2 Setup 3: "Range < 0.3% for 30+ candles".
COMPRESSION_LOOKBACK = 30
ATR_LENGTH = 14
# Higher timeframes folded from the 1m stream for chart reading. 15m gives the
# trend a 1m scalp is fighting or riding; 1h gives the structure that decides
# whether a 15m move is continuation or retracement.
DEFAULT_HTF = ("15m", "1h")


class SignalProcessor:
    def __init__(self, symbol: str, window: int = WINDOW,
                 htf_timeframes: tuple[str, ...] | list[str] = DEFAULT_HTF) -> None:
        self.symbol = symbol
        self.candles: deque[Candle] = deque(maxlen=window)
        self.prices: deque[float] = deque(maxlen=window)
        self.cvd_series: deque[float] = deque(maxlen=window)
        self.cvd_session = 0.0
        self.aggregators = [TimeframeAggregator(parse_timeframe(tf))
                            for tf in htf_timeframes]
        # Recomputed only when a higher-timeframe candle closes, not every 1m
        # candle: the reading cannot change until a new HTF bar exists, and
        # re-deriving EMAs and swings 15x more often than they can move is
        # pure waste on the hot path.
        self._htf: dict[str, HTFContext] = {}

    def _update_htf(self, candle: Candle) -> None:
        for agg in self.aggregators:
            if agg.update(candle) is not None:
                self._htf[agg.label] = ta.analyze(list(agg.closed), agg.label)

    def process(self, candle: Candle, funding_rate: float = 0.0,
                long_short_ratio: float = 1.0) -> SignalObject:
        self.candles.append(candle)
        self.prices.append(candle.close)
        self._update_htf(candle)

        delta = ind.delta_for(candle)
        self.cvd_session += delta
        self.cvd_series.append(self.cvd_session)

        candles = list(self.candles)
        vp = ind.volume_profile(candles)
        absorption = ind.detect_absorption(candles)
        divergence = ind.cvd_divergence(list(self.prices), list(self.cvd_series))
        phase = ind.amt_phase(candles, list(self.cvd_series))

        atr_value = osc.atr(candles, ATR_LENGTH)
        matches = pat.detect_patterns(candles)

        vols = np.array([c.volume for c in candles])
        avg_volume = float(vols.mean()) if len(vols) else candle.volume
        # Prior candles only — this one may be the breakout.
        prior = candles[-COMPRESSION_LOOKBACK - 1:-1]
        range_high = range_low = None
        range_pct = 0.0
        if len(prior) >= COMPRESSION_LOOKBACK:
            range_high = max(c.high for c in prior)
            range_low = min(c.low for c in prior)
            mid = (range_high + range_low) / 2
            range_pct = (range_high - range_low) / mid if mid else 0.0

        return SignalObject(
            timestamp=candle.timestamp,
            symbol=self.symbol,
            price=candle.close,
            high=candle.high,
            low=candle.low,
            cvd_1m=delta,
            cvd_session=self.cvd_session,
            cvd_divergence=divergence,
            volume_profile=vp,
            absorption=absorption,
            amt_phase=phase,
            funding_rate=funding_rate,
            long_short_ratio=long_short_ratio,
            volume=candle.volume,
            avg_volume=avg_volume,
            range_pct=range_pct,
            range_high=range_high,
            range_low=range_low,
            open=candle.open,
            htf=dict(self._htf),
            atr=atr_value,
            patterns=[m.name for m in matches],
            pattern_bias=pat.pattern_bias(matches),
        )
