"""Tests for the backtester: data conversion, metrics/gates, period rollover,
and an end-to-end run on the offline demo dataset."""
from maya.backtest import metrics as M
from maya.backtest.data import demo_dataset, from_csv, kline_to_candle
from maya.backtest.engine import Backtester
from maya.config import RunMode, Settings
from maya.risk.manager import RiskManager, SystemState


def test_kline_to_candle_approximates_buy_sell_split():
    # Close at the high => net buying (positive delta).
    c = kline_to_candle(0, 100, 110, 90, 110, 100)
    from maya.signals.indicators import candle_delta
    assert candle_delta(c.trades) > 0
    # Close at the low => net selling (negative delta).
    c2 = kline_to_candle(0, 100, 110, 90, 90, 100)
    assert candle_delta(c2.trades) < 0


def test_from_csv_with_header(tmp_path):
    p = tmp_path / "ohlcv.csv"
    p.write_text(
        "timestamp,open,high,low,close,volume\n"
        "1700000000000,100,110,95,105,1000\n"
        "1700000060000,105,108,104,106,500\n"
    )
    candles = list(from_csv(str(p)))
    assert len(candles) == 2
    assert candles[0].close == 105 and candles[1].volume == 500


def test_from_csv_headerless(tmp_path):
    p = tmp_path / "raw.csv"
    p.write_text("1700000000000,100,110,95,105,1000\n")
    candles = list(from_csv(str(p)))
    assert len(candles) == 1 and candles[0].high == 110


def test_metrics_gates():
    eq = [(i * 86_400_000, 1000 + i * 10) for i in range(10)]  # steady uptrend
    jm = {"trades": 250, "win_rate": 60.0, "profit_factor": 2.0,
          "total_pnl_usdt": 90.0, "expectancy": 0.36, "total_fees": 5.0}
    m = M.compute(jm, eq, start_equity=1000)
    assert m.trades == 250
    assert m.max_drawdown_pct == 0.0
    gate_names = {g.name: g.passed for g in m.gates}
    assert gate_names["Profit factor > 1.5"] is True
    assert gate_names["Trades >= 200"] is True


def test_metrics_max_drawdown():
    # peak 1200 then trough 900 => 25% drawdown
    eq = [(0, 1000), (1, 1200), (2, 900), (3, 1100)]
    m = M.compute({"trades": 0}, eq, start_equity=1000)
    assert abs(m.max_drawdown_pct - 25.0) < 1e-6


def test_risk_period_rollover_resets_daily_counters():
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000), 1000)
    day0 = 0
    rm.roll_periods(day0)
    rm.account.trades_today = 5
    rm.account.equity = 980
    # advance one day
    rm.roll_periods(day0 + 86_400_000 + 1)
    assert rm.account.trades_today == 0
    assert rm.account.day_start_equity == 980  # rebaselined to current equity


def test_rollover_lifts_daily_halt_but_killswitch_reblocks_on_floor():
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000), 1000)
    rm.roll_periods(0)
    rm.account.equity = 40  # below floor
    rm.check_kill_switch(0)
    assert rm.account.state is SystemState.HALTED
    # New day lifts the halt, but the floor breach must immediately re-halt.
    rm.roll_periods(86_400_000 + 1)
    rm.check_kill_switch(86_400_000 + 1)
    assert rm.account.state is SystemState.HALTED


def test_backtest_demo_runs_end_to_end():
    settings = Settings(mode=RunMode.PAPER, start_equity=1000, symbol="BTCUSDT")
    result = Backtester(settings).run(demo_dataset(days=20))
    assert result.metrics.trades > 0
    assert len(result.equity_curve) > 0
    assert len(result.metrics.gates) == 4
