"""Walk-forward / out-of-sample validation (spec §12.1 Phase 2, §14.1).

Splits the history into N sequential folds and runs an independent backtest on
each (fresh equity + risk state per fold). A strategy that is only profitable on
one regime shows up here as inconsistent across folds — the "consistent across
different market regimes" go/no-go criterion.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from maya.backtest.engine import Backtester
from maya.backtest.metrics import BacktestMetrics
from maya.config import Settings
from maya.data.models import Candle


@dataclass
class WalkForwardResult:
    folds: list[BacktestMetrics] = field(default_factory=list)
    profitable_folds: int = 0
    total_folds: int = 0
    consistent: bool = False
    total_trades: int = 0

    @property
    def pass_rate(self) -> float:
        return self.profitable_folds / self.total_folds if self.total_folds else 0.0


def run_walk_forward(settings: Settings, candles: list[Candle],
                     folds: int = 5, min_pass_rate: float = 0.6) -> WalkForwardResult:
    """Run `folds` sequential out-of-sample backtests over `candles`."""
    candles = list(candles)
    n = len(candles)
    if folds < 2 or n < folds * 2:
        raise ValueError(f"need >= {folds * 2} candles for {folds} folds, got {n}")

    size = n // folds
    result = WalkForwardResult(total_folds=folds)
    for i in range(folds):
        lo = i * size
        hi = n if i == folds - 1 else (i + 1) * size
        segment = candles[lo:hi]
        bt = Backtester(settings).run(segment)
        m = bt.metrics
        result.folds.append(m)
        result.total_trades += m.trades
        if m.total_pnl > 0:
            result.profitable_folds += 1
        logger.info("Fold {}/{}: {} trades, return {:.2f}%, PF {:.2f}, maxDD {:.2f}%",
                    i + 1, folds, m.trades, m.total_return_pct,
                    m.profit_factor, m.max_drawdown_pct)

    result.consistent = result.pass_rate >= min_pass_rate and result.total_trades > 0
    return result


def format_report(r: WalkForwardResult) -> str:
    lines = [
        "=" * 60,
        f"WALK-FORWARD VALIDATION — {r.total_folds} folds",
        "=" * 60,
        f"{'fold':>4}  {'trades':>7}  {'return%':>9}  {'PF':>7}  {'maxDD%':>7}  {'Sharpe':>7}",
    ]
    for i, m in enumerate(r.folds, 1):
        lines.append(f"{i:>4}  {m.trades:>7}  {m.total_return_pct:>9.2f}  "
                     f"{m.profit_factor:>7.2f}  {m.max_drawdown_pct:>7.2f}  {m.sharpe:>7.2f}")
    lines += [
        "-" * 60,
        f"Profitable folds : {r.profitable_folds}/{r.total_folds} "
        f"({r.pass_rate * 100:.0f}%)",
        f"Total trades     : {r.total_trades}",
        f"Consistency      : {'CONSISTENT' if r.consistent else 'INCONSISTENT'}",
        "-" * 60,
        "VERDICT: " + (
            "robust across folds — proceed to the full go/no-go gates"
            if r.consistent else
            "NOT robust — profitable folds below threshold; do not risk capital"),
        "=" * 60,
    ]
    return "\n".join(lines)
