"""Layer 1 — candle-boundary scheduler.

The naive way to poll for new candles is a tight loop that re-fetches every
few seconds and throws away most of the calls. :class:`CandleScheduler`
instead computes the next timeframe boundary and sleeps until just after it
closes, so :class:`~maya.data.ingestion.CCXTFeed` makes roughly one fetch per
candle instead of ~10, and the candle it gets back has actually just closed
rather than being caught mid-formation. On fetch errors it backs off
exponentially instead of hammering the exchange.
"""
from __future__ import annotations

import time

from loguru import logger

from maya.timeframe import timeframe_ms


class CandleScheduler:
    """Sleeps until the next candle-close boundary for ``timeframe``."""

    def __init__(self, timeframe: str, close_buffer_ms: int = 1_500,
                 max_backoff_s: float = 30.0) -> None:
        self.tf_ms = timeframe_ms(timeframe)
        # Small buffer after the boundary: the exchange needs a moment to
        # finalize/settle the closed kline before it's queryable.
        self.close_buffer_ms = close_buffer_ms
        self.max_backoff_s = max_backoff_s
        self._backoff_s = 1.0

    def next_boundary_ms(self, now_ms: int | None = None) -> int:
        """The next timeframe boundary strictly after ``now_ms``."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return ((now_ms // self.tf_ms) + 1) * self.tf_ms

    def ms_until_next_close(self, now_ms: int | None = None) -> int:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return max(0, self.next_boundary_ms(now_ms) + self.close_buffer_ms - now_ms)

    def sleep_until_next_close(self) -> None:
        wait_s = self.ms_until_next_close() / 1000
        if wait_s:
            time.sleep(wait_s)

    def on_success(self) -> None:
        self._backoff_s = 1.0

    def on_error(self, exc: Exception) -> None:
        logger.warning("candle fetch failed, retrying in {:.1f}s: {}", self._backoff_s, exc)
        time.sleep(self._backoff_s)
        self._backoff_s = min(self._backoff_s * 2, self.max_backoff_s)
