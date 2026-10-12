"""Layer 1/7 — data-feed watchdog (spec section 7.3, "data feed interrupted").

A stalled feed is the one failure the candle loop cannot see for itself: when
the exchange stops answering, the loop is blocked *inside* the feed waiting
for the next candle, so no check written in the loop ever runs. The watchdog
lives on its own thread, is told every time a candle arrives (:meth:`beat`),
and halts trading through the risk manager's kill switch once no candle has
arrived for one candle interval plus ``RiskLimits.DATA_STALE_SECONDS``.

Only the gap between arrivals is measured, never a candle's own age: at
startup the feed hands over the most recently *closed* candle, which on a 1h
timeframe can legitimately be most of an hour old.

The halt is sticky by design (see ``RiskManager``): a feed that went silent
once is a reason for a human to look before new trades are taken.
Positions already open keep their exchange-side stop and target.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from loguru import logger

from maya.config import RiskLimits
from maya.timeframe import timeframe_ms


class FeedWatchdog:
    def __init__(self, timeframe: str, on_stale: Callable[[float], None],
                 clock: Callable[[], float] = time.monotonic,
                 check_every_s: float = 5.0) -> None:
        self.limit_s = timeframe_ms(timeframe) / 1000 + RiskLimits.DATA_STALE_SECONDS
        self.on_stale = on_stale
        self.clock = clock
        self.check_every_s = check_every_s
        self._last = clock()
        # The first candle may take up to a whole interval to arrive (the feed
        # sleeps until the next close), so it gets twice the allowance.
        self._first = True
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """A candle arrived."""
        self._last = self.clock()
        self._first = False

    def silent_for(self) -> float:
        return self.clock() - self._last

    def check(self) -> bool:
        """Fire ``on_stale`` once if the feed has gone quiet. True if it fired."""
        allowance = self.limit_s * (2 if self._first else 1)
        gap = self.silent_for()
        if self._fired or gap <= allowance:
            return False
        self._fired = True
        logger.critical("Data feed silent for {:.0f}s (limit {:.0f}s) — halting new trades",
                        gap, allowance)
        self.on_stale(gap)
        return True

    def start(self) -> "FeedWatchdog":
        self._thread = threading.Thread(target=self._run, name="maya-feed-watchdog",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.check_every_s * 2)

    def _run(self) -> None:
        while not self._stop.wait(self.check_every_s):
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 - a watchdog must never die quietly
                logger.error("feed watchdog check failed: {}", exc)
