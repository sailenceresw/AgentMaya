"""Historical data loading & caching for the backtester.

Backtests run on **real order flow**, not a reconstruction. Binance's kline
endpoint returns the taker (aggressive) buy volume for each bar as field 9,
and every order-flow indicator here needs only the buy/sell split rather than
individual ticks:

- ``candle_delta`` / CVD  -> exactly ``2*taker_buy - volume``
- ``detect_absorption``   -> wicks and volume, i.e. OHLCV only
- ``volume_profile``      -> OHLCV only
- ``amt_phase``           -> OHLCV plus the CVD series

So one extra column buys genuine order flow, and the hundreds of millions of
aggTrade ticks a footprint chart would need are not required. ``ccxt``'s
``fetch_ohlcv`` normalises klines to six columns and discards that field,
which is why ``_fetch_raw_klines`` goes to the raw endpoint instead.

**Historical note:** this module used to synthesise each candle's ticks from
its direction with a fixed 0.62/0.38 bias. Every CVD-derived signal was then a
function of that generator rather than of the market, which made CVD
divergence and AMT phase untestable. Reconstruction now survives only as a
fallback for legacy caches and for the ``fetch_ohlcv`` degraded path; when it
is used, ``delta_for`` still works but the resulting CVD is an approximation
and should be treated as such.
"""
from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Iterator

from loguru import logger

from maya.config import Settings
from maya.data.models import Candle, Trade

from maya.timeframe import timeframe_ms

# Max candles per klines page. Binance USD-M documents 1500, but ccxt's
# binanceusdm caps a page at 1000, so that is the number that actually comes
# back. Requesting more and then treating a short page as end-of-data truncates
# every fetch to a single page.
_KLINE_LIMIT = 1000


def klines_cache_path(symbol: str, timeframe: str, start: str, end: str) -> str:
    """Where a date range of klines is cached. One definition, so the
    downloader, the backtester and the sweep always agree on the file."""
    return f"data/klines_{symbol}_{timeframe}_{start}_{end}.csv"


def funding_cache_path(symbol: str, start: str, end: str) -> str:
    return f"data/funding_{symbol}_{start}_{end}.csv"


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
    tf_ms = timeframe_ms(settings.timeframe)
    rows: list[list] = []
    since = start_ms
    while since < end_ms:
        batch = _fetch_raw_klines(ex, settings, since)
        if not batch:
            break  # no data left (e.g. range predates the symbol's listing)
        rows.extend(batch)
        # Advance past the last candle received. Never assume a full batch means
        # "more data" and a short one means "done": the venue caps a page below
        # whatever we asked for, so a short page is normal mid-range. The
        # ``since < end_ms`` condition is what ends the loop; this guard only
        # catches an exchange that stops advancing, which would spin forever.
        next_since = int(batch[-1][0]) + tf_ms
        if next_since <= since:
            break
        since = next_since
        if len(rows) % (_KLINE_LIMIT * 20) < _KLINE_LIMIT:
            logger.info("  ...{} candles so far", len(rows))
    rows = [r for r in rows if r[0] <= end_ms]
    logger.info("Fetched {} historical candles for {} {}", len(rows),
               settings.symbol, settings.timeframe)

    if cache_path and rows:
        _save_csv(rows, cache_path)
    return rows


def _fetch_raw_klines(ex, settings: Settings, since: int) -> list[list]:
    """One page of klines as ``[ts, o, h, l, c, v, taker_buy_base_volume]``.

    Deliberately not ``ccxt.fetch_ohlcv``: that normalises to six columns and
    throws away field 9, the taker (aggressive) buy volume. That single number
    is what makes CVD real rather than reconstructed, so it is worth going to
    the raw endpoint for. Falls back to the normalised call if the implicit
    API is unavailable, in which case the taker column comes back as None and
    the caller degrades to tick reconstruction.
    """
    params = {"symbol": settings.symbol.replace("/", ""),
              "interval": settings.timeframe,
              "startTime": since, "limit": _KLINE_LIMIT}
    try:
        raw = ex.fapiPublicGetKlines(params)
    except Exception as exc:  # noqa: BLE001 - any ccxt/network/schema issue
        logger.warning("raw klines unavailable ({}); falling back to fetch_ohlcv "
                       "-- CVD will be reconstructed, not real", exc)
        return [list(r) + [None] for r in
                ex.fetch_ohlcv(settings.symbol, timeframe=settings.timeframe,
                               since=since, limit=_KLINE_LIMIT)]
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
             float(r[5]), float(r[9])] for r in raw]


def _save_csv(rows: list[list], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume",
                    "taker_buy_volume"])
        w.writerows(rows)
    logger.info("Cached {} candles to {}", len(rows), path)


def _load_csv(path: str) -> list[list]:
    """Load cached rows, tolerating caches written before the taker-buy column
    existed — those simply come back with None and fall back to tick data."""
    with open(path, newline="") as f:
        r = csv.reader(f)
        header = next(r)
        has_taker = len(header) > 6
        out: list[list] = []
        for row in r:
            base = [int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                    float(row[4]), float(row[5])]
            tbv = float(row[6]) if has_taker and row[6] not in ("", "None") else None
            out.append(base + [tbv])
        return out


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
    """Turn cached rows into Candles.

    When the row carries the exchange's taker-buy volume, the candle gets real
    order flow and no ticks are invented at all. Only rows from a legacy cache
    (or a fetch that fell back to ``fetch_ohlcv``) still get reconstructed
    ticks — see the module docstring.
    """
    rng = random.Random(seed)
    for row in rows:
        ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        tbv = row[6] if len(row) > 6 else None
        if tbv is not None:
            yield Candle(timestamp=int(ts), open=o, high=h, low=l, close=c,
                         volume=v, taker_buy_volume=tbv)
        else:
            yield Candle(timestamp=int(ts), open=o, high=h, low=l, close=c,
                         volume=v, trades=_synthetic_trades(rng, int(ts), o, c, v))
