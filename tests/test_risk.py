"""Tests for the risk layer — the most safety-critical code (spec section 7)."""
from maya.config import RiskLimits, RunMode, Settings
from maya.risk.manager import RiskManager, SystemState
from maya.risk.sizing import calculate_position_notional
from maya.strategy.models import TradeIntent


def _settings():
    return Settings(mode=RunMode.SIM, start_equity=1000)


def test_position_sizing_caps_at_notional_limit():
    # Tiny stop distance would imply huge notional -> must be capped at 5x equity.
    notional = calculate_position_notional(1000, 0.5, entry=100, sl=99.99)
    assert notional <= 1000 * RiskLimits.POSITION_NOTIONAL_CAP_MULT


def test_position_sizing_respects_risk_pct():
    notional = calculate_position_notional(1000, 0.5, entry=100, sl=95)
    # risk = 1000 * 0.005 = 5 USDT; sl distance = 5% => notional = 100
    assert abs(notional - 100) < 1e-6


def _intent(rr=5.0):
    return TradeIntent("BTCUSDT", "long", "value_area_absorption",
                       entry_price=100, stop_loss=95, take_profit_1=120,
                       take_profit_2=125, confluence_score=8, expected_rr=rr)


def test_rejects_low_rr():
    rm = RiskManager(_settings(), 1000)
    decision = rm.screen(_intent(rr=2.0), now_ts=0)
    assert not decision.approved


def test_rejects_when_halted():
    rm = RiskManager(_settings(), 1000)
    rm.manual_halt()
    assert rm.screen(_intent(), now_ts=0).approved is False


def test_daily_loss_limit_triggers_halt():
    rm = RiskManager(_settings(), 1000)
    rm.account.equity = 1000 * (1 - 0.04)  # -4% > 3% daily limit
    rm.check_kill_switch(now_ts=0)
    assert rm.account.state is SystemState.HALTED


def test_max_one_position():
    rm = RiskManager(_settings(), 1000)
    rm.on_trade_opened()
    assert rm.screen(_intent(), now_ts=0).approved is False
