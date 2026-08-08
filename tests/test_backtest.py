"""Tests for the backtester — metrics math and the runner reusing the real
pipeline over synthetic/scripted candles (no network needed)."""
from maya.backtest.metrics import BacktestMetrics, compute_metrics, _max_drawdown_pct, _sharpe_annualized
from maya.backtest.runner import run_backtest
from maya.config import RunMode, Settings
from maya.data.ingestion import SimulatedFeed
from maya.data.scenarios import ScenarioFeed


def _row(pnl: float, result: str):
    return {"pnl_usdt": pnl, "result": result}


def test_max_drawdown_from_flat_equity_is_zero():
    assert _max_drawdown_pct([1000, 1000, 1000]) == 0.0


def test_max_drawdown_picks_worst_peak_to_trough():
    # peak 1200 -> trough 900 is the worst drawdown, not 1000 -> 900
    series = [1000, 1200, 1100, 900, 950]
    assert abs(_max_drawdown_pct(series) - (300 / 1200 * 100)) < 1e-9


def test_sharpe_zero_for_constant_returns_with_no_variance():
    assert _sharpe_annualized([0.01, 0.01, 0.01]) == 0.0


def test_sharpe_positive_for_consistently_positive_returns():
    assert _sharpe_annualized([0.01, 0.02, 0.015, 0.008]) > 0


def test_compute_metrics_profit_factor_and_gate():
    rows = [_row(50, "win"), _row(-10, "loss"), _row(60, "win")]
    daily_equity = {0: 1000, 86_400_000: 1100}
    m = compute_metrics(daily_equity, rows, start_equity=1000)
    assert m.trades == 3
    assert abs(m.win_rate - (2 / 3 * 100)) < 0.01
    assert abs(m.profit_factor - (110 / 10)) < 1e-6
    gate = m.passes_gate()
    assert gate["trades >= 200"] is False  # only 3 trades here


def test_compute_metrics_handles_zero_trades():
    m = compute_metrics({0: 1000}, [], start_equity=1000)
    assert m.trades == 0
    assert m.win_rate == 0.0
    assert m.expectancy == 0.0


def test_backtest_runner_replays_scenario_and_matches_live_run():
    """The scenario feed produces one winning trade live (test_integration.py);
    the backtester must reproduce the exact same result since it reuses the
    same unmodified pipeline."""
    settings = Settings(mode=RunMode.SIM, scenario=True, start_equity=1000, max_candles=0)
    metrics = run_backtest(settings, ScenarioFeed(settings).candles())
    assert metrics.trades == 1
    assert metrics.win_rate == 100.0
    assert metrics.total_pnl_usdt > 0
    assert metrics.end_equity > metrics.start_equity


def test_backtest_runner_handles_a_run_with_no_trades():
    settings = Settings(mode=RunMode.SIM, start_equity=1000, max_candles=50)
    metrics = run_backtest(settings, SimulatedFeed(settings, seed=1).candles())
    assert metrics.trades >= 0
    assert metrics.start_equity == 1000


def test_gate_all_pass_when_criteria_met():
    m = BacktestMetrics(trades=250, win_rate=45, profit_factor=2.0,
                        total_pnl_usdt=500, start_equity=1000, end_equity=1500,
                        max_drawdown_pct=10.0, sharpe_annualized=1.5, expectancy=2.0)
    assert all(m.passes_gate().values())


def test_gate_fails_on_drawdown_even_if_profitable():
    m = BacktestMetrics(trades=250, win_rate=45, profit_factor=2.0,
                        total_pnl_usdt=500, start_equity=1000, end_equity=1500,
                        max_drawdown_pct=25.0, sharpe_annualized=1.5, expectancy=2.0)
    gate = m.passes_gate()
    assert gate["max_drawdown < 20%"] is False
    assert not all(gate.values())
