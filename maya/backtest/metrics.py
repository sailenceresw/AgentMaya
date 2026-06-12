"""Backtest performance metrics and go/no-go gate evaluation (spec §9.2, §14.1)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Gate:
    name: str
    value: float
    threshold: float
    passed: bool
    op: str  # ">" or "<"


@dataclass
class BacktestMetrics:
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    expectancy: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    avg_rr: float = 0.0
    total_fees: float = 0.0
    start_equity: float = 0.0
    final_equity: float = 0.0
    gates: list[Gate] = field(default_factory=list)

    @property
    def all_gates_pass(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)


def _max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    arr = np.asarray(equity_curve, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = (peak - arr) / np.where(peak == 0, 1, peak)
    return float(dd.max() * 100)


def _sharpe(daily_equity: list[float]) -> float:
    if len(daily_equity) < 3:
        return 0.0
    eq = np.asarray(daily_equity, dtype=float)
    rets = np.diff(eq) / eq[:-1]
    sd = rets.std(ddof=1)
    # Degenerate near-zero variance (e.g. perfectly consistent synthetic wins)
    # would otherwise produce an absurd Sharpe from float noise. Treat a
    # vanishing std relative to the mean as undefined, and clamp the result.
    if sd < 1e-9 or sd < abs(rets.mean()) * 1e-6:
        return 0.0
    return float(np.clip(rets.mean() / sd * np.sqrt(365), -100.0, 100.0))


def compute(journal_metrics: dict, equity_ts: list[tuple[int, float]],
            start_equity: float) -> BacktestMetrics:
    """Combine journal trade stats with equity-curve analytics."""
    m = BacktestMetrics(start_equity=start_equity)
    m.trades = journal_metrics.get("trades", 0)
    if m.trades:
        m.win_rate = journal_metrics["win_rate"]
        m.profit_factor = journal_metrics["profit_factor"]
        m.total_pnl = journal_metrics["total_pnl_usdt"]
        m.expectancy = journal_metrics["expectancy"]
        m.total_fees = journal_metrics["total_fees"]

    equities = [e for _, e in equity_ts]
    m.final_equity = equities[-1] if equities else start_equity
    m.total_return_pct = (m.final_equity - start_equity) / start_equity * 100
    m.max_drawdown_pct = _max_drawdown(equities)

    # Resample equity to one point per day for a stable Sharpe.
    daily: dict[int, float] = {}
    for ts, eq in equity_ts:
        daily[ts // 86_400_000] = eq
    m.sharpe = _sharpe([daily[k] for k in sorted(daily)])

    # Go/No-Go gates (spec §14.1).
    m.gates = [
        Gate("Profit factor > 1.5", m.profit_factor, 1.5, m.profit_factor > 1.5, ">"),
        Gate("Max drawdown < 20%", m.max_drawdown_pct, 20.0, m.max_drawdown_pct < 20.0, "<"),
        Gate("Sharpe > 1.0", m.sharpe, 1.0, m.sharpe > 1.0, ">"),
        Gate("Trades >= 200", m.trades, 200, m.trades >= 200, ">"),
    ]
    return m


def format_report(m: BacktestMetrics) -> str:
    lines = [
        "=" * 60,
        "BACKTEST RESULTS",
        "=" * 60,
        f"Trades            : {m.trades}",
        f"Win rate          : {m.win_rate:.1f}%",
        f"Profit factor     : {m.profit_factor:.2f}",
        f"Total PnL         : ${m.total_pnl:.2f}",
        f"Total return      : {m.total_return_pct:.2f}%",
        f"Expectancy/trade  : ${m.expectancy:.2f}",
        f"Max drawdown      : {m.max_drawdown_pct:.2f}%",
        f"Sharpe (annual)   : {m.sharpe:.2f}",
        f"Total fees        : ${m.total_fees:.2f}",
        f"Equity            : ${m.start_equity:.2f} -> ${m.final_equity:.2f}",
        "-" * 60,
        "GO / NO-GO GATES (spec §14.1):",
    ]
    for g in m.gates:
        mark = "PASS" if g.passed else "FAIL"
        lines.append(f"  [{mark}] {g.name}  (actual: {g.value:.2f})")
    verdict = "GO — proceed to paper trading" if m.all_gates_pass else \
        "NO-GO — do not risk capital; refine strategy/data"
    lines += ["-" * 60, f"VERDICT: {verdict}", "=" * 60]
    return "\n".join(lines)
