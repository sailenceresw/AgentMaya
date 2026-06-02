"""Historical data sources for the backtester.

Three sources:

* ``csv``    — load OHLCV from a CSV file (Binance kline dumps or simple OHLCV).
* ``ccxt``   — download history from Binance via paginated ``fetch_ohlcv``
               (needs network access to Binance).
* ``demo``   — a self-contained, offline dataset (the scripted scenario repeated
               across many days) so the backtester runs with no data/network.
               It is a demo fixture, NOT a market model.

Klines carry only OHLCV, not per-trade data, so we approximate order flow:
buy/sell volume is split by where the close sits within the candle range (a
standard proxy). True order-flow backtests need tick/aggTrade data (spec §4.2).
"""
from __future__ import annotations

import csv as _csv
from typing import Iterator

from loguru import logger

from maya.config import Settings
from maya.data.models import Candle, Trade

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def kline_to_candle(ts: int, o: float, h: float, l: float, c: float, v: float) -> Candle:
    """Build a Candle from an OHLCV row, approximating buy/sell split for CVD."""
    rng = (h - l) or 1e-9
    buy_frac = max(0.0, min(1.0, (c - l) / rng))  # close near high => net buying
    buy_vol = v * buy_frac
    sell_vol = v * (1.0 - buy_frac)
    # Encode the split as two aggregate trades (is_buyer_maker True == sell).
    trades = [
        Trade(ts, c, buy_vol, is_buyer_maker=False),
        Trade(ts, c, sell_vol, is_buyer_maker=True),
    ]
    return Candle(ts, o, h, l, c, v, trades)


def from_csv(path: str) -> Iterator[Candle]:
    """Yield candles from a CSV. Accepts a header row or raw Binance klines.

    Recognised columns (case-insensitive): timestamp/open_time, open, high, low,
    close, volume. Falls back to positional columns 0..5 for headerless files.
    """
    with open(path, newline="") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        has_header = any(ch.isalpha() for ch in sample.splitlines()[0]) if sample else False
        reader = _csv.reader(fh)
        rows = list(reader)

    start = 1 if has_header else 0
    header = [h.strip().lower() for h in rows[0]] if has_header else []

    def idx(*names, default):
        for n in names:
            if n in header:
                return header.index(n)
        return default

    i_ts = idx("timestamp", "open_time", "time", default=0)
    i_o = idx("open", default=1)
    i_h = idx("high", default=2)
    i_l = idx("low", default=3)
    i_c = idx("close", default=4)
    i_v = idx("volume", default=5)

    for row in rows[start:]:
        if not row or len(row) <= i_v:
            continue
        yield kline_to_candle(
            int(float(row[i_ts])), float(row[i_o]), float(row[i_h]),
            float(row[i_l]), float(row[i_c]), float(row[i_v]),
        )


def from_ccxt(settings: Settings, days: int) -> Iterator[Candle]:
    """Download `days` of history from Binance via ccxt (paginated)."""
    from maya.data.binance_client import build_exchange, to_ccxt_symbol

    ex = build_exchange(settings)
    ex.load_markets()
    sym = to_ccxt_symbol(settings.symbol)
    tf = settings.timeframe
    tf_ms = _TF_MS.get(tf, 60_000)
    since = ex.milliseconds() - days * 86_400_000
    logger.info("Downloading {} {} candles since {} days ago ...", sym, tf, days)
    while True:
        batch = ex.fetch_ohlcv(sym, timeframe=tf, since=since, limit=1500)
        if not batch:
            break
        for row in batch:
            yield kline_to_candle(int(row[0]), float(row[1]), float(row[2]),
                                  float(row[3]), float(row[4]), float(row[5]))
        since = batch[-1][0] + tf_ms
        if len(batch) < 1500 or since >= ex.milliseconds():
            break


def demo_dataset(days: int = 250) -> Iterator[Candle]:
    """Offline demo data: the scripted scenario replayed once per day.

    Produces roughly one trade per day so the backtester and its metrics can be
    exercised end-to-end with no network. NOT representative of real markets.
    """
    from maya.config import RunMode
    from maya.data.scenarios import ScenarioFeed

    base = 1_600_000_000_000
    template = list(ScenarioFeed(Settings(mode=RunMode.SIM)).candles())
    span = len(template) * 60_000
    for d in range(days):
        day_start = base + d * 86_400_000
        for k, c in enumerate(template):
            ts = day_start + k * 60_000
            yield Candle(ts, c.open, c.high, c.low, c.close, c.volume,
                         [Trade(ts, t.price, t.qty, t.is_buyer_maker) for t in c.trades])
        _ = span  # readability: each day holds one full scenario


def build_source(settings: Settings, source: str, csv_path: str | None,
                 days: int) -> Iterator[Candle]:
    if source == "csv":
        if not csv_path:
            raise ValueError("--csv PATH is required for source=csv")
        logger.info("Loading candles from {}", csv_path)
        return from_csv(csv_path)
    if source == "ccxt":
        return from_ccxt(settings, days)
    logger.info("Using offline demo dataset ({} days). Not a market model.", days)
    return demo_dataset(days)
