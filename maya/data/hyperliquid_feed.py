"""Layer 1 — Hyperliquid candle feed.

Polls Hyperliquid's public ``candles_snapshot`` for the configured coin and
timeframe and yields closed candles. Klines carry no per-trade data, so CVD is
approximated from where each candle closes in its range (same proxy the
backtester uses); a future upgrade can aggregate the websocket ``trades`` feed
for true order flow.
"""
from __future__ import annotations

import time
from typing import Iterator

from loguru import logger

from maya.config import Settings
from maya.data.models import Candle

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def _hl_candle_to_candle(row: dict) -> Candle:
    from maya.backtest.data import kline_to_candle
    return kline_to_candle(
        int(row["t"]), float(row["o"]), float(row["h"]),
        float(row["l"]), float(row["c"]), float(row["v"]),
    )


class HyperliquidFeed:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.coin = settings.coin
        self.interval = settings.timeframe
        self.tf_ms = _TF_MS.get(self.interval, 60_000)
        from maya.data.hyperliquid_client import build_info
        self.info = build_info(settings)
        self._last_ts: int | None = None

    def _snapshot(self, start: int, end: int) -> list[dict]:
        try:
            return self.info.candles_snapshot(self.coin, self.interval, start, end) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("candles_snapshot failed: {}", exc)
            return []

    def candles(self) -> Iterator[Candle]:
        emitted = 0
        limit = self.settings.max_candles or 10**9
        while emitted < limit:
            now = int(time.time() * 1000)
            rows = self._snapshot(now - 3 * self.tf_ms, now)
            if len(rows) < 2:
                time.sleep(1)
                continue
            row = rows[-2]  # last *closed* candle (rows[-1] may be forming)
            ts = int(row["t"])
            if ts == self._last_ts:
                time.sleep(self.tf_ms / 1000 / 10)
                continue
            self._last_ts = ts
            emitted += 1
            yield _hl_candle_to_candle(row)


def download_candles(settings: Settings, days: int) -> Iterator[Candle]:
    """Historical Hyperliquid candles for backtesting (paginated)."""
    from maya.data.hyperliquid_client import build_info
    info = build_info(settings)
    coin = settings.coin
    tf_ms = _TF_MS.get(settings.timeframe, 60_000)
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    cursor = start
    logger.info("Downloading {} {} candles from Hyperliquid ...", coin, settings.timeframe)
    while cursor < end:
        batch = info.candles_snapshot(coin, settings.timeframe, cursor,
                                      min(cursor + 5000 * tf_ms, end)) or []
        if not batch:
            break
        for row in batch:
            yield _hl_candle_to_candle(row)
        last_t = int(batch[-1]["t"])
        if last_t + tf_ms <= cursor:
            break
        cursor = last_t + tf_ms
