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
from maya.signals.models import SignalObject

WINDOW = 60  # candles retained for profile / phase calculations


class SignalProcessor:
    def __init__(self, symbol: str, window: int = WINDOW) -> None:
        self.symbol = symbol
        self.candles: deque[Candle] = deque(maxlen=window)
        self.prices: deque[float] = deque(maxlen=window)
        self.cvd_series: deque[float] = deque(maxlen=window)
        self.cvd_session = 0.0

    def process(self, candle: Candle, funding_rate: float = 0.0,
                long_short_ratio: float = 1.0) -> SignalObject:
        self.candles.append(candle)
        self.prices.append(candle.close)

        delta = ind.candle_delta(candle.trades)
        self.cvd_session += delta
        self.cvd_series.append(self.cvd_session)

        candles = list(self.candles)
        vp = ind.volume_profile(candles)
        absorption = ind.detect_absorption(candles)
        divergence = ind.cvd_divergence(list(self.prices), list(self.cvd_series))
        phase = ind.amt_phase(candles, list(self.cvd_series))

        vols = np.array([c.volume for c in candles])
        avg_volume = float(vols.mean()) if len(vols) else candle.volume
        closes = np.array([c.close for c in candles])
        range_pct = float((closes.max() - closes.min()) / max(closes.mean(), 1e-9)) if len(closes) > 1 else 0.0

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
        )
