"""Download historical klines from Binance into a CSV for backtesting.

Usage:
    python -m maya.data.download --days 365 --symbol BTCUSDT --timeframe 1m --out btc.csv

Needs network access to Binance (see docs/RUNNING.md for region/egress notes).
The CSV it writes is consumed by `python -m maya.backtest --source csv --csv FILE`.
"""
from __future__ import annotations

import argparse
import csv
import sys

from loguru import logger

from maya.config import RunMode, Settings
from maya.data.binance_client import build_exchange, to_ccxt_symbol

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def download(settings: Settings, days: int, out_path: str) -> int:
    """Fetch `days` of klines and write them to `out_path`. Returns row count."""
    ex = build_exchange(settings)
    ex.load_markets()
    sym = to_ccxt_symbol(settings.symbol)
    tf = settings.timeframe
    tf_ms = _TF_MS.get(tf, 60_000)
    since = ex.milliseconds() - days * 86_400_000
    now = ex.milliseconds()

    written = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        while since < now:
            batch = ex.fetch_ohlcv(sym, timeframe=tf, since=since, limit=1500)
            if not batch:
                break
            for row in batch:
                w.writerow([int(row[0]), row[1], row[2], row[3], row[4], row[5]])
            written += len(batch)
            since = batch[-1][0] + tf_ms
            logger.info("  ...{} candles (through {})", written,
                        _iso(ex, batch[-1][0]))
            if len(batch) < 1500:
                break
    return written


def _iso(ex, ms: int) -> str:
    try:
        return ex.iso8601(ms)
    except Exception:  # noqa: BLE001
        return str(ms)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="maya.data.download",
                                description="Download Binance klines to CSV")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--out", required=True, help="Output CSV path")
    p.add_argument("--testnet", action="store_true", help="Use the testnet endpoint")
    args = p.parse_args(argv)

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | {message}")

    settings = Settings(
        mode=RunMode.TESTNET if args.testnet else RunMode.PAPER,
        symbol=args.symbol, timeframe=args.timeframe,
    )
    logger.info("Downloading {} {} {} days -> {}", args.symbol, args.timeframe,
                args.days, args.out)
    try:
        n = download(settings, args.days, args.out)
    except Exception as exc:  # noqa: BLE001
        logger.error("Download failed: {}", exc)
        logger.error("If this is a network/region block, run on a host with "
                     "Binance access, or download monthly dumps from "
                     "https://data.binance.vision and point --source csv at them.")
        sys.exit(1)
    logger.info("Done — wrote {} candles to {}", n, args.out)


if __name__ == "__main__":
    main()
