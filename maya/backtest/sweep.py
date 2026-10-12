"""Which coins and timeframes, if any, pay after fees? Measure before trading.

Runs the standard backtester — the real LangGraph pipeline, the real risk
manager with its fee-aware gate, and the venue's real fee model — over every
``symbol x timeframe`` pair and prints one row each:

    symbol   tf   trades  win%   PF   gross$   fees$  net$  fees/gross  maxDD%  verdict

Data is Binance USD-M's public klines (free, no key, real taker-buy volume),
cached to ``data/`` so re-runs are instant.

    MAYA_VENUE=hyperliquid python -m maya.backtest.sweep \\
        --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 5m,15m,1h \\
        --start 2025-01-01 --end 2025-12-31

A pair is only worth trading if its **net** row survives — and a single good
row across many pairs is what chance alone produces, so prefer pairs that
hold up across several, and confirm on a later date range before trusting
any of them (see the README's measurement notes).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from maya.backtest.data_loader import (
    fetch_historical_klines,
    historical_candles,
    klines_cache_path,
)
from maya.backtest.metrics import BacktestMetrics
from maya.backtest.runner import run_backtest
from maya.config import Settings, get_settings


@dataclass
class SweepRow:
    symbol: str
    timeframe: str
    metrics: BacktestMetrics | None
    error: str = ""

    @property
    def verdict(self) -> str:
        m = self.metrics
        if m is None:
            return f"ERROR {self.error}"
        if m.trades < 30:
            return "too few trades to judge"
        if m.total_pnl_usdt <= 0:
            share = m.fee_share_of_gross
            if share is not None and share >= 1:
                return "fees ate it: profitable before fees, losing after"
            return "loses money"
        if m.profit_factor < 1.3:
            return "marginal"
        return "candidate — confirm out of sample"


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def sweep(base: Settings, symbols: list[str], timeframes: list[str],
          start: str, end: str, loader=fetch_historical_klines,
          plot_dir: str | None = None) -> list[SweepRow]:
    rows = []
    for tf in timeframes:
        for symbol in symbols:
            settings = base.model_copy(update={"symbol": symbol, "timeframe": tf,
                                               "mode": base.mode.SIM, "llm_enabled": False})
            try:
                klines = loader(settings, _ms(start), _ms(end),
                                cache_path=klines_cache_path(symbol, tf, start, end))
                if not klines:
                    rows.append(SweepRow(symbol, tf, None, "no data"))
                    continue
                metrics = run_backtest(settings, historical_candles(klines),
                                       total=len(klines), progress_every=0)
                rows.append(SweepRow(symbol, tf, metrics))
                if plot_dir and metrics.equity_curve:
                    _plot(metrics, plot_dir, symbol, tf, start, end)
            except Exception as exc:  # noqa: BLE001 - one pair must not end the sweep
                rows.append(SweepRow(symbol, tf, None, str(exc)[:60]))
            logger.info("{} {} done", symbol, tf)
    return rows


def _plot(metrics: BacktestMetrics, plot_dir: str, symbol: str, tf: str,
          start: str, end: str) -> None:
    from pathlib import Path

    from maya.backtest.plot import save_equity_plot

    Path(plot_dir).mkdir(parents=True, exist_ok=True)
    try:
        save_equity_plot(metrics, f"{plot_dir}/{symbol}_{tf}.png",
                         title=f"{symbol} {tf} {start}→{end}")
    except (ValueError, RuntimeError) as exc:
        logger.warning("no chart for {} {}: {}", symbol, tf, exc)


def format_rows(rows: list[SweepRow]) -> str:
    out = [f"{'symbol':<12}{'tf':<5}{'trades':>7}{'win%':>7}{'PF':>6}{'gross$':>10}"
           f"{'fees$':>9}{'net$':>10}{'fees/gross':>11}{'maxDD%':>8}  verdict"]
    for r in rows:
        m = r.metrics
        if m is None:
            out.append(f"{r.symbol:<12}{r.timeframe:<5}{'':>68}  {r.verdict}")
            continue
        share = m.fee_share_of_gross
        out.append(
            f"{r.symbol:<12}{r.timeframe:<5}{m.trades:>7}{m.win_rate:>7.1f}"
            f"{m.profit_factor:>6.2f}{m.gross_pnl_usdt:>10.2f}{m.total_fees:>9.2f}"
            f"{m.total_pnl_usdt:>10.2f}{('n/a' if share is None else f'{share:.0%}'):>11}"
            f"{m.max_drawdown_pct:>8.1f}  {r.verdict}")
    return "\n".join(out)


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    p.add_argument("--timeframes", default="5m,15m,1h")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--plot-dir", default=None,
                   help="write an equity/drawdown PNG per pair here (needs matplotlib)")
    a = p.parse_args()

    base = get_settings()
    rows = sweep(base, [s.strip() for s in a.symbols.split(",") if s.strip()],
                 [t.strip() for t in a.timeframes.split(",") if t.strip()], a.start, a.end,
                 plot_dir=a.plot_dir)
    print(f"\nvenue={base.venue.value}  fees: round trip "
          f"{base.effective_fee_rate * 2:.3%} (worst case)\n")
    print(format_rows(rows))


if __name__ == "__main__":
    main()
