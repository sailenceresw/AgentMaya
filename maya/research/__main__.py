"""Research harness CLI.

    python -m maya.research list
    python -m maya.research run <hypothesis_id> [--symbols ...] [--timeframe 15m]
    python -m maya.research holdout <hypothesis_id>
    python -m maya.research registry
    python -m maya.research failures
    python -m maya.research diagnose <hypothesis_id>   (research window only, no registry row)

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
from maya.research.registry import HoldoutAlreadyUsed, HypothesisRegistry, make_lesson
from maya.research.stats import corrected_threshold


DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT"
DEFAULT_START = "2024-08-10"
DEFAULT_END = "2026-08-09"


def _window(a, hyp) -> tuple[str, str]:
    start = a.start or (hyp.start if hyp is not None else "") or DEFAULT_START
    end = a.end or (hyp.end if hyp is not None else "") or DEFAULT_END
    return start, end


def _symbols(arg: str | None, hyp) -> list[str]:
    raw = arg or (hyp.symbols if hyp is not None else "") or DEFAULT_SYMBOLS
    return [s.strip() for s in raw.split(",") if s.strip()]


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
    p.add_argument("command", choices=["list", "run", "holdout", "registry", "failures",
                                       "diagnose"])
    p.add_argument("hypothesis_id", nargs="?")
    p.add_argument("--symbols", default=None,
                   help="defaults to the hypothesis's declared coins, else " + DEFAULT_SYMBOLS)
    p.add_argument("--timeframe", default=None,
                   help="bar size; defaults to the one the hypothesis declares")
    p.add_argument("--start", default=None,
                   help="defaults to the hypothesis's declared window, else " + DEFAULT_START)
    p.add_argument("--end", default=None,
                   help="defaults to the hypothesis's declared window, else " + DEFAULT_END)
    p.add_argument("--db", default="maya_journal.db")
    p.add_argument("--merge", action="store_true",
                   help="diagnose: combine per-coin runs (--symbols A then B ...) into one report")
    p.add_argument("--rerun", action="store_true",
                   help="test an id that already has a research row (adds another row)")
    a = p.parse_args(argv)

    if a.command == "list":
        for h in CATALOG.values():
            flag = "  [blocked: " + h.blocked + "]" if h.blocked else ""
            print(f"{h.id:<28}{h.cost:<7}{h.timeframe:<5}{h.description}{flag}")
        return 0

    if a.command == "diagnose":
        # Exploration, not a test: research window only, and the registry is
        # never opened, so looking cannot write a row or raise the threshold.
        from maya.research.diagnose import diagnose
        from maya.research.hypotheses import PIPELINE_CONFIGS
        if a.hypothesis_id not in PIPELINE_CONFIGS:
            print(f"diagnose supports the pipeline hypotheses: "
                  f"{', '.join(sorted(PIPELINE_CONFIGS))}", file=sys.stderr)
            return 2
        hyp = CATALOG[a.hypothesis_id]
        if hyp.blocked:
            print(f"blocked: {hyp.id} cannot run yet: {hyp.blocked}", file=sys.stderr)
            return 4
        symbols = _symbols(a.symbols, hyp)
        if a.merge:
            from maya.research.diagnose import merge
            try:
                print(merge(hyp.id, symbols))
            except FileNotFoundError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            return 0
        data = load_data(symbols, a.timeframe or hyp.timeframe, *_window(a, hyp), loader=loader)
        # The holdout cutoff is the newest loaded candle minus 120 days. Coins
        # cached over the same --start/--end share that cutoff, so diagnosing
        # one coin at a time sees the same research window as all together.
        print(diagnose(hyp.id, data.research))
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

    if a.command == "failures":
        failed = registry.failures()
        if not failed:
            print("no failed hypotheses on record")
        for r in failed:
            print(r.lesson or make_lesson(r.id, r.reason, r.kind))
        return 0

    if a.hypothesis_id not in CATALOG:
        print(f"unknown hypothesis {a.hypothesis_id!r}; try: python -m maya.research list",
              file=sys.stderr)
        return 2
    hyp = CATALOG[a.hypothesis_id]
    if hyp.blocked:
        print(f"blocked: {hyp.id} cannot be tested yet: {hyp.blocked}", file=sys.stderr)
        return 4
    if hyp.precheck is not None and (why := hyp.precheck()):
        print(f"cannot run {hyp.id} here: {why}", file=sys.stderr)
        return 4
    earlier = registry.research_rows_for(hyp.id)
    if a.command == "run" and earlier and not a.rerun:
        last = earlier[-1]
        verdict = "passed" if last.passed_gate else (last.lesson or "failed: " + last.reason)
        print(f"refused: {hyp.id} was already tested ({verdict}). A rerun on the same data "
              "gives the same answer and raises the threshold for everything after it. "
              "Use --rerun if the data or the code changed.", file=sys.stderr)
        return 3
    if a.command == "run":
        failed = registry.failures()
        if failed:
            print("past failures, so this is not one of them under a new name:")
            for r in failed:
                print("  " + (r.lesson or make_lesson(r.id, r.reason, r.kind)))
    data = load_data(_symbols(a.symbols, hyp), a.timeframe or hyp.timeframe,
                     *_window(a, hyp), loader=loader)
    try:
        rep = (run_research if a.command == "run" else run_holdout)(hyp, data, registry)
    except (HoldoutAlreadyUsed, PermissionError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(rep.format())
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
