"""The symbol x timeframe sweep: plumbing and verdicts, with offline data."""
from __future__ import annotations

from maya.backtest.metrics import BacktestMetrics
from maya.backtest.sweep import SweepRow, format_rows, sweep
from maya.config import Settings, Venue
from maya.data.scenarios import ScenarioFeed


def _m(**kw) -> BacktestMetrics:
    base = dict(trades=100, win_rate=40.0, profit_factor=1.6, total_pnl_usdt=120.0,
                start_equity=1000, end_equity=1120, max_drawdown_pct=8.0,
                sharpe_annualized=1.2, expectancy=1.2, total_fees=30.0)
    base.update(kw)
    return BacktestMetrics(**base)


def test_fee_share_and_gross_are_derived_from_net_plus_fees():
    m = _m(total_pnl_usdt=20.0, total_fees=60.0)
    assert m.gross_pnl_usdt == 80.0
    assert m.fee_share_of_gross == 0.75


def test_verdicts():
    assert SweepRow("X", "1m", _m(trades=10)).verdict == "too few trades to judge"
    assert SweepRow("X", "1m", _m(total_pnl_usdt=-5.0, total_fees=40.0)).verdict.startswith(
        "fees ate it")
    assert SweepRow("X", "1m", _m(total_pnl_usdt=-50.0, total_fees=10.0)).verdict == "loses money"
    assert SweepRow("X", "1m", _m(profit_factor=1.1)).verdict == "marginal"
    assert SweepRow("X", "1h", _m()).verdict.startswith("candidate")


def test_sweep_runs_every_pair_through_the_real_backtester():
    scenario = list(ScenarioFeed(Settings(scenario=True)).candles())
    rows_in = [[c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.volume * 0.5]
               for c in scenario]
    seen = []

    def loader(settings, start_ms, end_ms, cache_path=None):
        seen.append((settings.symbol, settings.timeframe))
        return rows_in

    rows = sweep(Settings(venue=Venue.HYPERLIQUID), ["BTCUSDT", "ETHUSDT"], ["15m", "1h"],
                 "2025-01-01", "2025-02-01", loader=loader)
    assert seen == [("BTCUSDT", "15m"), ("ETHUSDT", "15m"), ("BTCUSDT", "1h"), ("ETHUSDT", "1h")]
    assert all(r.metrics is not None for r in rows)
    assert "fees$" in format_rows(rows)


def test_a_failing_pair_is_reported_not_fatal():
    def loader(settings, *a, **k):
        raise RuntimeError("symbol not listed")

    rows = sweep(Settings(), ["NOPEUSDT"], ["1h"], "2025-01-01", "2025-02-01", loader=loader)
    assert rows[0].metrics is None and "symbol not listed" in rows[0].verdict
