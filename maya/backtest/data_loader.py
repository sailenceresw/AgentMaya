"""Historical data loading & caching for the backtester.

Binance's REST klines endpoint gives real OHLCV, but not the underlying
trade-tick data the order-flow signal layer needs (CVD, absorption, delta).
Getting genuine historical aggTrade data at the scale of 12+ months
(hundreds of millions of ticks) is a materially bigger data-engineering job
than this backtester covers. So — same approach as the existing
``SimulatedFeed`` — each candle's trades are **reconstructed synthetically**
from its OHLCV shape and a buy/sell bias derived from candle direction.

**This means CVD/absorption signals in backtests are an approximation of
real order flow, not the genuine article.** Treat backtest results as
directional (does the setup structure hold up across regimes, roughly what
does the R:R distribution look like) rather than a precise forecast of live
performance. This is exactly why the spec's own paper-trading gate (14.2)
requires live results within 30% of backtest expectations before going
further — it assumes the backtest is directionally right, not exact.
"""
from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Iterator

from loguru import logger

from maya.config import Settings
from maya.data.models import Candle, Trade

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def fetch_historical_klines(settings: Settings, start_ms: int, end_ms: int,
                             cache_path: str | None = None) -> list[list]:
    """Fetch raw OHLCV rows [ts, o, h, l, c, v] from Binance REST, paginated.
    Public endpoint — no API keys needed. Caches to CSV if a path is given so
    repeated backtests over the same range don't re-download."""
    if cache_path and Path(cache_path).exists():
        logger.info("Loading cached klines from {}", cache_path)
        return _load_csv(cache_path)

    import ccxt  # imported lazily — not needed unless actually backtesting

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
    tf_ms = _TF_MS.get(settings.timeframe, 60_000)
    rows: list[list] = []
    since = start_ms
    while since < end_ms:
        batch = ex.fetch_ohlcv(settings.symbol, timeframe=settings.timeframe,
                               since=since, limit=1500)
        if not batch:
            break
        rows.extend(batch)
        since = int(batch[-1][0]) + tf_ms
        if len(batch) < 1500:
            break
    rows = [r for r in rows if r[0] <= end_ms]
    logger.info("Fetched {} historical candles for {} {}", len(rows),
               settings.symbol, settings.timeframe)

    if cache_path and rows:
        _save_csv(rows, cache_path)
    return rows


def _save_csv(rows: list[list], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    logger.info("Cached {} candles to {}", len(rows), path)


def _load_csv(path: str) -> list[list]:
    with open(path, newline="") as f:
        r = csv.reader(f)
        next(r)  # header
        return [[int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                 float(row[4]), float(row[5])] for row in r]


def _synthetic_trades(rng: random.Random, ts: int, o: float, c: float, vol: float) -> list[Trade]:
    n = rng.randint(8, 20)
    bias = 0.62 if c >= o else 0.38  # P(aggressive buy) — same heuristic as SimulatedFeed
    trades = []
    for k in range(n):
        px = o + (c - o) * (k / max(n - 1, 1)) + rng.gauss(0, abs(c - o) / 5 + 1)
        qty = abs(rng.gauss(vol / n, vol / n / 2)) + 1e-6
        is_buyer_maker = rng.random() > bias
        trades.append(Trade(timestamp=ts, price=px, qty=qty, is_buyer_maker=is_buyer_maker))
    return trades


def historical_candles(rows: list[list], seed: int = 7) -> Iterator[Candle]:
    """Turn raw OHLCV rows into Candles with reconstructed (synthetic)
    trades — see the module docstring for why they're synthetic, not real."""
    rng = random.Random(seed)
    for ts, o, h, l, c, v in rows:
        yield Candle(timestamp=int(ts), open=o, high=h, low=l, close=c, volume=v,
                    trades=_synthetic_trades(rng, int(ts), o, c, v))
