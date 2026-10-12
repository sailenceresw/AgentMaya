"""Performance metrics for the backtest gate (spec section 14.1).

Everything here operates on the *realized* equity curve (equity as of each
closed trade / each day), not a full mark-to-market intra-trade curve —
AccountState only updates ``equity`` on trade close, so drawdown between a
trade's entry and its close isn't captured. This is a known approximation:
worse-case intratrade drawdown will always be understated relative to a true
tick-by-tick equity curve.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class BacktestMetrics:
    trades: int
    win_rate: float
    profit_factor: float
    total_pnl_usdt: float
    start_equity: float
    end_equity: float
    max_drawdown_pct: float
    sharpe_annualized: float
    expectancy: float
    # Round-trip fees charged across all trades. PnL above is already net of
    # them; gross = total_pnl_usdt + total_fees.
    total_fees: float = 0.0

    @property
    def gross_pnl_usdt(self) -> float:
        return round(self.total_pnl_usdt + self.total_fees, 2)

    @property
    def fee_share_of_gross(self) -> float | None:
        """Fraction of the pre-fee profit the fees took (None if no gross profit)."""
        gross = self.total_pnl_usdt + self.total_fees
        return self.total_fees / gross if gross > 0 else None

    def passes_gate(self) -> dict[str, bool]:
        """Spec section 14.1 — the go/no-go criteria before paper trading."""
        return {
            "profit_factor > 1.5": self.profit_factor > 1.5,
            "max_drawdown < 20%": self.max_drawdown_pct < 20.0,
            "sharpe > 1.0 (annualized)": self.sharpe_annualized > 1.0,
            "trades >= 200": self.trades >= 200,
        }


def _max_drawdown_pct(equity_series: list[float]) -> float:
    if not equity_series:
        return 0.0
    peak = equity_series[0]
    max_dd = 0.0
    for e in equity_series:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100)
    return max_dd


def _sharpe_annualized(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(365)


def compute_metrics(daily_equity: dict[int, float], trade_rows: list,
                     start_equity: float) -> BacktestMetrics:
    """
    daily_equity: {utc_day_start_ms: equity_as_of_that_day} — one point per
                  day the backtest actually processed a candle for.
    trade_rows:   sqlite3.Row objects from TradeJournal's `trades` table.
    """
    days_sorted = sorted(daily_equity)
    equity_series = [daily_equity[d] for d in days_sorted] or [start_equity]
    end_equity = equity_series[-1]

    daily_returns = []
    for prev, cur in zip(equity_series, equity_series[1:], strict=False):
        if prev > 0:
            daily_returns.append((cur - prev) / prev)

    n = len(trade_rows)
    wins = [r for r in trade_rows if r["result"] == "win"]
    losses = [r for r in trade_rows if r["result"] == "loss"]
    gross_profit = sum(r["pnl_usdt"] for r in wins)
    gross_loss = -sum(r["pnl_usdt"] for r in losses)
    total_pnl = sum(r["pnl_usdt"] for r in trade_rows)

    return BacktestMetrics(
        trades=n,
        win_rate=round(len(wins) / n * 100, 2) if n else 0.0,
        profit_factor=round(gross_profit / gross_loss, 2) if gross_loss else float("inf"),
        total_pnl_usdt=round(total_pnl, 2),
        start_equity=start_equity,
        end_equity=round(end_equity, 2),
        max_drawdown_pct=round(_max_drawdown_pct(equity_series), 2),
        sharpe_annualized=round(_sharpe_annualized(daily_returns), 2),
        expectancy=round(total_pnl / n, 2) if n else 0.0,
        total_fees=round(sum((r["fees_usdt"] or 0.0) for r in trade_rows
                             if "fees_usdt" in r.keys()), 2),
    )
