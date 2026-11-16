"""Research harness CLI.

    python -m maya.research list
    python -m maya.research run <hypothesis_id> [--symbols ...] [--timeframe 15m]
    python -m maya.research holdout <hypothesis_id>
    python -m maya.research registry

Data is Binance USD-M public klines, cached under ``data/`` (see
``maya.data.download``). The last 120 days of the range are the holdout.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from maya.backtest.data_loader import (
    fetch_historical_klines,
    historical_candles,
    klines_cache_path,
)
from maya.config import get_settings
from maya.research.harness import ResearchData, run_holdout, run_research
from maya.research.hypotheses import CATALOG
from maya.research.registry import HoldoutAlreadyUsed, HypothesisRegistry
from maya.research.stats import corrected_threshold


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def load_data(symbols: list[str], timeframe: str, start: str, end: str,
              loader=fetch_historical_klines) -> ResearchData:
    base = get_settings()
    candles = {}
    for symbol in symbols:
        settings = base.model_copy(update={"symbol": symbol, "timeframe": timeframe})
        rows = loader(settings, _ms(start), _ms(end),
                      cache_path=klines_cache_path(symbol, timeframe, start, end))
        candles[symbol] = list(historical_candles(rows))
    return ResearchData(candles)


def main(argv: list[str] | None = None, loader=fetch_historical_klines) -> int:
    p = argparse.ArgumentParser(prog="python -m maya.research", description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["list", "run", "holdout", "registry"])
    p.add_argument("hypothesis_id", nargs="?")
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--start", default="2024-08-10")
    p.add_argument("--end", default="2026-08-09")
    p.add_argument("--db", default="maya_journal.db")
    a = p.parse_args(argv)

    if a.command == "list":
        for h in CATALOG.values():
            print(f"{h.id:<28}{h.cost:<7}{h.description}")
        return 0

    registry = HypothesisRegistry(a.db)
    if a.command == "registry":
        rows = registry.rows()
        print(f"{len(rows)} rows; next research test needs |t| >= "
              f"{corrected_threshold(registry.count_tests() + 1):.2f}")
        for r in rows:
            t = "  n/a" if r.t_stat is None else f"{r.t_stat:+.2f}"
            print(f"{r.id:<28}{r.kind:<9}{t:>7}  {'PASS' if r.passed_gate else 'FAIL'}  {r.reason}")
        return 0

    if a.hypothesis_id not in CATALOG:
        print(f"unknown hypothesis {a.hypothesis_id!r}; try: python -m maya.research list",
              file=sys.stderr)
        return 2
    hyp = CATALOG[a.hypothesis_id]
    data = load_data([s.strip() for s in a.symbols.split(",") if s.strip()],
                     a.timeframe, a.start, a.end, loader=loader)
    try:
        rep = (run_research if a.command == "run" else run_holdout)(hyp, data, registry)
    except (HoldoutAlreadyUsed, PermissionError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(rep.format())
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
