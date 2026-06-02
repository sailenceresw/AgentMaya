"""CLI: python -m maya.backtest [--source demo|csv|ccxt] [...]"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

from maya.backtest.data import build_source
from maya.backtest.engine import Backtester
from maya.backtest.metrics import format_report
from maya.config import RunMode, Settings


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="maya.backtest", description="Backtest the Maya strategy")
    p.add_argument("--source", choices=["demo", "csv", "ccxt"], default="demo",
                   help="demo=offline fixture (default), csv=file, ccxt=download from Binance")
    p.add_argument("--csv", help="Path to OHLCV CSV (for --source csv)")
    p.add_argument("--days", type=int, default=250, help="History length for ccxt/demo")
    p.add_argument("--symbol", default=None)
    p.add_argument("--timeframe", default=None)
    p.add_argument("--equity", type=float, default=None, help="Starting equity (USDT)")
    args = p.parse_args(argv)

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")

    overrides = {"mode": RunMode.PAPER}
    if args.symbol:
        overrides["symbol"] = args.symbol
    if args.timeframe:
        overrides["timeframe"] = args.timeframe
    if args.equity is not None:
        overrides["start_equity"] = args.equity
    settings = Settings(**overrides)

    try:
        candles = build_source(settings, args.source, args.csv, args.days)
        result = Backtester(settings).run(candles)
    except Exception as exc:  # noqa: BLE001
        logger.error("Backtest failed: {}", exc)
        if args.source == "ccxt":
            logger.error("If this is a network/region block, download a CSV instead "
                         "and use --source csv (see docs/RUNNING.md).")
        sys.exit(1)

    if args.source == "demo":
        print("\n" + "!" * 60)
        print("! DEMO DATA — the scripted scenario replayed across days.")
        print("! These numbers are MEANINGLESS for judging real profitability;")
        print("! they only prove the backtest machinery runs. Use --source csv")
        print("! or --source ccxt with REAL Binance history to evaluate the edge.")
        print("!" * 60)

    print(format_report(result.metrics))
    sys.exit(0)  # report-only; never errors on NO-GO


if __name__ == "__main__":
    main()
