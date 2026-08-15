"""Higher-timeframe candle aggregation.

The agent ingests 1-minute candles. Everything the strategy reads off a "chart"
above that — 15m trend, 1h structure — is built here by folding the 1m stream
into higher-timeframe candles, rather than fetching a second data feed.

Doing it this way matters for correctness, not just convenience:

- **One clock.** A separate 15m feed can arrive late or early relative to the
  1m stream, and reconciling that is a class of bug that shows up as
  signals firing on the wrong bar. Folding one stream cannot desynchronise.
- **No lookahead.** A higher-timeframe candle is only published once a 1m
  candle from the *next* bucket arrives, so a closed 15m candle is never
  visible before the minute it actually completed. Backtest and live see the
  same thing at the same time, which is the property the whole
  backtest≡live design rests on.
"""
from __future__ import annotations

from collections import deque

from maya.data.models import Candle
from maya.timeframe import label_for, parse_timeframe  # re-exported


class TimeframeAggregator:
    """Folds 1m candles into one higher timeframe.

    Buckets are aligned to the epoch (``ts // bucket_ms``), which is how the
    exchange itself aligns candles — a 15m bar always starts at :00, :15, :30
    or :45, never 15 minutes after whenever the process happened to start.
    """

    def __init__(self, minutes: int, maxlen: int = 300) -> None:
        if minutes < 1:
            raise ValueError("minutes must be >= 1")
        self.minutes = minutes
        self.bucket_ms = minutes * 60_000
        self.closed: deque[Candle] = deque(maxlen=maxlen)
        self.forming: Candle | None = None
        self._bucket_ts: int | None = None

    @property
    def label(self) -> str:
        return label_for(self.minutes)

    def update(self, candle: Candle) -> Candle | None:
        """Fold in one 1m candle.

        Returns the higher-timeframe candle that *just closed*, or None if the
        current bucket is still filling. The forming candle is kept separately
        and is deliberately not returned: acting on a partially built bar is
        the most common way a multi-timeframe strategy accidentally looks
        ahead of itself.
        """
        bucket = (candle.timestamp // self.bucket_ms) * self.bucket_ms
        completed: Candle | None = None

        if self._bucket_ts is None:
            self._start(bucket, candle)
            return None

        if bucket != self._bucket_ts:
            # A candle from a later bucket proves the previous one is complete.
            completed = self.forming
            if completed is not None:
                self.closed.append(completed)
            self._start(bucket, candle)
            return completed

        self._merge(candle)
        return None

    def _start(self, bucket: int, candle: Candle) -> None:
        self._bucket_ts = bucket
        self.forming = Candle(
            timestamp=bucket, open=candle.open, high=candle.high,
            low=candle.low, close=candle.close, volume=candle.volume,
        )

    def _merge(self, candle: Candle) -> None:
        f = self.forming
        assert f is not None  # _bucket_ts set => forming set
        f.high = max(f.high, candle.high)
        f.low = min(f.low, candle.low)
        f.close = candle.close
        f.volume += candle.volume
