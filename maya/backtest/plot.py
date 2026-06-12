"""Render a backtest equity curve + drawdown to a PNG.

matplotlib is imported lazily (and the Agg backend forced) so the rest of the
backtester has no plotting dependency.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from maya.backtest.metrics import BacktestMetrics


def save_equity_plot(equity_curve: list[tuple[int, float]],
                     metrics: BacktestMetrics, path: str) -> None:
    """Write an equity-curve + drawdown chart to ``path`` (PNG)."""
    if not equity_curve:
        raise ValueError("empty equity curve — nothing to plot")

    import matplotlib
    matplotlib.use("Agg")  # headless / no display
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    ts = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t, _ in equity_curve]
    eq = np.asarray([e for _, e in equity_curve], dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.where(peak == 0, 1, peak) * 100

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ax1.plot(ts, eq, color="#1f77b4", lw=1.4, label="Equity")
    ax1.plot(ts, peak, color="#2ca02c", lw=0.8, ls="--", alpha=0.6, label="Peak")
    ax1.fill_between(ts, eq, metrics.start_equity, where=eq >= metrics.start_equity,
                     color="#2ca02c", alpha=0.08)
    ax1.fill_between(ts, eq, metrics.start_equity, where=eq < metrics.start_equity,
                     color="#d62728", alpha=0.08)
    ax1.axhline(metrics.start_equity, color="grey", lw=0.8, alpha=0.5)
    ax1.set_ylabel("Equity (USDT)")
    ax1.legend(loc="upper left", fontsize=8)
    verdict = "GO" if metrics.all_gates_pass else "NO-GO"
    ax1.set_title(
        f"Maya backtest — {metrics.trades} trades · "
        f"return {metrics.total_return_pct:.1f}% · PF {metrics.profit_factor:.2f} · "
        f"maxDD {metrics.max_drawdown_pct:.1f}% · Sharpe {metrics.sharpe:.2f} · {verdict}",
        fontsize=10,
    )

    ax2.fill_between(ts, -dd, 0, color="#d62728", alpha=0.4)
    ax2.set_ylabel("Drawdown %")
    ax2.set_xlabel("Time (UTC)")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
