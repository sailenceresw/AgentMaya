"""Fetch backtest data once, then backtest and sweep offline.

Writes to the exact cache files ``maya.backtest.runner`` and
``maya.backtest.sweep`` read (``data/klines_<SYMBOL>_<TF>_<START>_<END>.csv``),
so a later run finds them and touches no network.

From Binance's public API (no key):

    python -m maya.data.download --symbols BTCUSDT,ETHUSDT,SOLUSDT \\
        --timeframes 15m,1h --start 2025-01-01 --end 2025-12-31

Where the Binance API is blocked, from the monthly dump files published at
https://data.binance.vision (USD-M futures: data/futures/um/monthly/klines/
<SYMBOL>/<TF>/) — download the .zip files by browser, then:

    python -m maya.data.download --import-dumps ~/Downloads/BTCUSDT-1h-2025-*.zip \\
        --symbols BTCUSDT --timeframes 1h --start 2025-01-01 --end 2025-12-31

Both paths keep the taker-buy volume column, so CVD stays real order flow.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from maya.backtest.data_loader import (
    _save_csv,
    fetch_historical_klines,
    funding_cache_path,
    klines_cache_path,
)
from maya.config import RunMode, Settings

# data.binance.vision kline columns: open_time, open, high, low, close,
# volume, close_time, quote_volume, count, taker_buy_volume,
# taker_buy_quote_volume, ignore.
_TAKER_BUY_COL = 9


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def parse_dump_rows(text: str) -> list[list]:
    """Rows ``[ts, o, h, l, c, v, taker_buy]`` from one dump CSV's text.

    Tolerates the header row newer files carry, and microsecond timestamps
    (Binance switched some dumps from ms to µs in 2025)."""
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if not rec or not rec[0].strip().lstrip("-").isdigit():
            continue                      # header or blank
        ts = int(rec[0])
        if ts > 10**14:                   # microseconds -> milliseconds
            ts //= 1000
        taker = float(rec[_TAKER_BUY_COL]) if len(rec) > _TAKER_BUY_COL else None
        rows.append([ts, float(rec[1]), float(rec[2]), float(rec[3]),
                     float(rec[4]), float(rec[5]), taker])
    return rows


def import_dumps(paths: list[str], start_ms: int, end_ms: int) -> list[list]:
    """Merge dump files (.zip or .csv), keep [start, end], de-duplicate, sort."""
    by_ts: dict[int, list] = {}
    for p in paths:
        path = Path(p).expanduser()
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                texts = [zf.read(n).decode() for n in zf.namelist() if n.endswith(".csv")]
        else:
            texts = [path.read_text()]
        for text in texts:
            for row in parse_dump_rows(text):
                if start_ms <= row[0] <= end_ms:
                    by_ts[row[0]] = row
    return [by_ts[t] for t in sorted(by_ts)]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="maya.data.download", description=__doc__.split("\n")[0])
    p.add_argument("--symbols", default="BTCUSDT")
    p.add_argument("--timeframes", default="1h")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--import-dumps", nargs="+", metavar="FILE",
                   help="data.binance.vision .zip/.csv files instead of the API "
                        "(one symbol and timeframe per call)")
    p.add_argument("--no-funding", action="store_true", help="skip funding history")
    a = p.parse_args(argv)

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in a.timeframes.split(",") if t.strip()]
    start_ms, end_ms = _ms(a.start), _ms(a.end)

    if a.import_dumps:
        if len(symbols) != 1 or len(timeframes) != 1:
            p.error("--import-dumps takes exactly one --symbols and one --timeframes value")
        rows = import_dumps(a.import_dumps, start_ms, end_ms)
        if not rows:
            logger.error("no candles in {} between {} and {}", a.import_dumps, a.start, a.end)
            sys.exit(1)
        out = klines_cache_path(symbols[0], timeframes[0], a.start, a.end)
        _save_csv(rows, out)
        logger.info("Imported {} candles -> {}", len(rows), out)
        return

    failed = 0
    for symbol in symbols:
        for tf in timeframes:
            settings = Settings(mode=RunMode.PAPER, symbol=symbol, timeframe=tf)
            out = klines_cache_path(symbol, tf, a.start, a.end)
            try:
                rows = fetch_historical_klines(settings, start_ms, end_ms, cache_path=out)
                logger.info("{} {}: {} candles -> {}", symbol, tf, len(rows), out)
            except Exception as exc:  # noqa: BLE001 - keep going with the others
                failed += 1
                logger.error("{} {} failed: {} — if Binance is blocked here, use "
                             "--import-dumps with files from data.binance.vision",
                             symbol, tf, exc)
        if not a.no_funding:
            from maya.backtest.funding import fetch_funding_rates
            try:
                fetch_funding_rates(Settings(mode=RunMode.PAPER, symbol=symbol), start_ms,
                                    end_ms, cache_path=funding_cache_path(symbol, a.start, a.end))
            except Exception as exc:  # noqa: BLE001
                logger.warning("{} funding history unavailable: {}", symbol, exc)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
