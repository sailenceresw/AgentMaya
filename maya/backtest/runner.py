"""Event-driven backtester (spec section 12.1 Phase 2, gate section 14.1).

Runs candles through the exact same LangGraph pipeline used live —
``maya/graph.py`` and ``maya/nodes.py`` are not touched here at all — so a
backtest result is directly comparable to paper/live results. There is no
separate backtest-only strategy implementation to quietly drift out of sync
with the real one, which is one of the most common ways backtests end up
lying to you.

Run directly:

    MAYA_SYMBOL=BTCUSDT MAYA_TIMEFRAME=1m \\
    MAYA_BACKTEST_START=2025-01-01 MAYA_BACKTEST_END=2025-12-31 \\
    python -m maya.backtest.runner

See ``maya/backtest/data_loader.py`` for an important caveat: backtest
trades are volume-shape reconstructions, not real historical tick data, so
CVD/absorption signals here are an approximation.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Iterator

from loguru import logger

from maya.backtest.data_loader import (
    fetch_historical_klines,
    funding_cache_path,
    historical_candles,
    klines_cache_path,
)
from maya.backtest.funding import FundingSchedule, fetch_funding_rates
from maya.backtest.metrics import BacktestMetrics, compute_metrics
from maya.config import Settings, get_settings
from maya.data.models import Candle
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.execution.engine import flatten_on_shutdown
from maya.nodes import TradingNodes

_DAY_MS = 86_400_000


def run_backtest(settings: Settings, candles: Iterator[Candle],
                  journal_path: str = ":memory:",
                  total: int | None = None,
                  progress_every: int = 5_000,
                  funding=None) -> BacktestMetrics:
    journal = TradeJournal(db_path=journal_path)
    nodes = TradingNodes(settings, journal, equity=settings.start_equity, now_ts=0,
                         funding=funding)
    graph = build_graph(nodes)

    daily_equity: dict[int, float] = {}
    started = time.monotonic()
    n = 0
    last: Candle | None = None
    period_halts = 0
    was_halted = False
    stopped_reason = ""
    for candle in candles:
        last = candle
        graph.invoke({"candle": candle})
        n += 1
        if progress_every and n % progress_every == 0:
            # A months-long 1m backtest is a multi-hour run; without this it
            # prints nothing at all until the final report.
            elapsed = time.monotonic() - started
            rate = n / elapsed
            eta = f", eta {(total - n) / rate / 60:.0f}m" if total else ""
            logger.info("  {}{} candles | {:.0f}/s | equity ${:.2f}{}",
                        n, f"/{total}" if total else "", rate,
                        nodes.risk.account.equity, eta)
        day = (candle.timestamp // _DAY_MS) * _DAY_MS
        daily_equity[day] = nodes.risk.account.equity
        halted = nodes.risk.account.state.value == "halted"
        if halted and nodes.risk.halt_is_sticky:
            stopped_reason = ", ".join(nodes.risk.account.halt_reasons)
            logger.warning("Backtest halted at candle {}{} ({})", n,
                           f"/{total}" if total else "", stopped_reason)
            break
        # A daily or weekly loss halt is not the end of the test. This loop
        # used to `break` on any halt, so the first bad day ended the run:
        # with risk sizing, two years of SOL 15m stopped after 33% of the
        # candles and reported the remainder as if it had been measured. The
        # risk manager refuses entries while halted and lifts the halt when
        # the period rolls — the same thing a restarted live process does —
        # so the candles keep coming and the halt is counted instead.
        if halted and not was_halted:
            period_halts += 1
        was_halted = halted

    if nodes.execution.has_position and last is not None:
        # Mark to the last close the backtest saw, on the day it saw it.
        result = flatten_on_shutdown(nodes.execution, nodes.risk, journal,
                                     last.close, last.timestamp)
        if result:
            day = (last.timestamp // _DAY_MS) * _DAY_MS
            daily_equity[day] = nodes.risk.account.equity

    trade_rows = journal.conn.execute(
        "SELECT * FROM trades ORDER BY timestamp_exit"
    ).fetchall()
    metrics = compute_metrics(daily_equity, trade_rows, settings.start_equity)
    metrics.candles_processed = n
    metrics.candles_total = total if total is not None else n
    metrics.period_halts = period_halts
    metrics.stopped_reason = stopped_reason
    journal.close()
    return metrics


def _print_report(m: BacktestMetrics, start: str, end: str) -> None:
    logger.info("=== Backtest Report: {} -> {} ===", start, end)
    logger.info("Trades            : {}", m.trades)
    logger.info("Win rate          : {}%", m.win_rate)
    logger.info("Profit factor     : {}", m.profit_factor)
    logger.info("Total PnL         : ${:.2f} (${:.2f} -> ${:.2f})",
                m.total_pnl_usdt, m.start_equity, m.end_equity)
    logger.info("Max drawdown      : {:.2f}%", m.max_drawdown_pct)
    logger.info("Sharpe (annual)   : {:.2f}", m.sharpe_annualized)
    logger.info("Expectancy/trade  : ${:.2f}", m.expectancy)
    logger.info("Data covered      : {}/{} candles ({:.0%}), {} daily/weekly loss halt(s)",
                m.candles_processed, m.candles_total, m.coverage, m.period_halts)
    if m.stopped_reason:
        logger.warning("Stopped early     : {} — everything after it is unmeasured",
                       m.stopped_reason)
    logger.info("--- Spec 14.1 backtesting gate ---")
    gate = m.passes_gate()
    for criterion, ok in gate.items():
        logger.info("  [{}] {}", "PASS" if ok else "FAIL", criterion)
    logger.info("Overall           : {}", "GO" if all(gate.values()) else "NO-GO")
    logger.warning("Reminder: signal data is reconstructed, not real historical "
                   "ticks — see maya/backtest/data_loader.py docstring. Treat this "
                   "as directional, not a guaranteed live forecast.")


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    settings = get_settings()
    start = os.environ.get("MAYA_BACKTEST_START", "2025-01-01")
    end = os.environ.get("MAYA_BACKTEST_END", "2025-12-31")
    cache = os.environ.get(
        "MAYA_BACKTEST_CACHE",
        klines_cache_path(settings.symbol, settings.timeframe, start, end),
    )

    try:
        start_ms = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError as exc:
        logger.critical("MAYA_BACKTEST_START/END must be ISO dates (YYYY-MM-DD): {}", exc)
        sys.exit(2)

    rows = fetch_historical_klines(settings, start_ms, end_ms, cache_path=cache)
    if not rows:
        logger.critical("No historical data available for the requested range.")
        sys.exit(2)

    funding_cache = os.environ.get(
        "MAYA_FUNDING_CACHE",
        funding_cache_path(settings.symbol, start, end),
    )
    funding = FundingSchedule(
        fetch_funding_rates(settings, start_ms, end_ms, cache_path=funding_cache))
    logger.info("Funding schedule: {} settlements", len(funding))

    metrics = run_backtest(settings, historical_candles(rows), total=len(rows),
                           funding=funding)
    _print_report(metrics, start, end)
    plot_path = os.environ.get("MAYA_BACKTEST_PLOT")
    if plot_path:
        from maya.backtest.plot import save_equity_plot
        try:
            save_equity_plot(metrics, plot_path,
                             title=f"{settings.symbol} {settings.timeframe} {start}→{end}")
            logger.info("Equity chart written to {}", plot_path)
        except (ValueError, RuntimeError) as exc:
            logger.warning("no chart: {}", exc)
    sys.exit(0 if all(metrics.passes_gate().values()) else 1)


if __name__ == "__main__":
    main()
