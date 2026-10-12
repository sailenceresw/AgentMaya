"""Equity curve + drawdown chart for a backtest, as a PNG.

    MAYA_BACKTEST_PLOT=backtest.png python -m maya.backtest.runner
    python -m maya.backtest.sweep ... --plot-dir charts/

matplotlib is optional (``pip install matplotlib``) and imported only here,
with the headless Agg backend, so the backtester itself never depends on it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from maya.backtest.metrics import BacktestMetrics


def save_equity_plot(metrics: BacktestMetrics, path: str, title: str = "") -> None:
    """Write ``metrics.equity_curve`` and its drawdown to ``path``."""
    if not metrics.equity_curve:
        raise ValueError("empty equity curve — nothing to plot")
    try:
        import matplotlib
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("charts need matplotlib: pip install matplotlib") from exc
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    ts = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t, _ in metrics.equity_curve]
    eq = np.asarray([e for _, e in metrics.equity_curve], dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.where(peak == 0, 1, peak) * 100
    start = metrics.start_equity

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(ts, eq, color="#1f77b4", lw=1.4, label="Equity (after fees)")
    ax1.plot(ts, peak, color="#2ca02c", lw=0.8, ls="--", alpha=0.6, label="Peak")
    ax1.fill_between(ts, eq, start, where=eq >= start, color="#2ca02c", alpha=0.08)
    ax1.fill_between(ts, eq, start, where=eq < start, color="#d62728", alpha=0.08)
    ax1.axhline(start, color="grey", lw=0.8, alpha=0.5)
    ax1.set_ylabel("Equity (USDT)")
    ax1.legend(loc="upper left", fontsize=8)

    pf = "∞" if not np.isfinite(metrics.profit_factor) else f"{metrics.profit_factor:.2f}"
    verdict = "GO" if all(metrics.passes_gate().values()) else "NO-GO"
    ret = (metrics.end_equity - start) / start * 100 if start else 0.0
    head = f"{title} — " if title else "Maya backtest — "
    ax1.set_title(
        f"{head}{metrics.trades} trades · return {ret:.1f}% · PF {pf} · "
        f"fees ${metrics.total_fees:.2f} · maxDD {metrics.max_drawdown_pct:.1f}% · "
        f"Sharpe {metrics.sharpe_annualized:.2f} · {verdict}", fontsize=10)

    ax2.fill_between(ts, -dd, 0, color="#d62728", alpha=0.4)
    ax2.set_ylabel("Drawdown %")
    ax2.set_xlabel("Time (UTC)")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
