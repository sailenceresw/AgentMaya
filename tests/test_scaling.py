"""Tests for partial take-profit scaling in the execution engine (spec §8.3)."""
from maya.config import RunMode, Settings
from maya.data.models import Candle
from maya.execution.engine import ExecutionEngine
from maya.risk.manager import RiskDecision
from maya.strategy.models import TradeIntent


def _engine():
    return ExecutionEngine(Settings(mode=RunMode.SIM, start_equity=1000))


def _candle(ts, o, h, l, c):
    return Candle(ts, o, h, l, c, 100, [])


def test_scale_out_three_tranches_long():
    eng = _engine()
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption",
                         entry_price=100.0, stop_loss=99.0, take_profit_1=102.0,
                         take_profit_2=104.0, confluence_score=8, expected_rr=4.0)
    dec = RiskDecision(True, "ok", position_notional=1000.0, quantity=10.0, leverage=5)
    assert eng.open(intent, dec, ts=0)
    pos = eng.position

    # TP1 hit -> 50% closed, stop to breakeven, still open.
    r = eng.manage(_candle(60_000, 100, 102.5, 100, 102.0))
    assert r is None
    assert pos.tp1_done and abs(pos.remaining_qty - 5.0) < 1e-9
    assert pos.stop_loss == pos.entry_price  # breakeven

    # TP2 hit -> another 25% closed, trailing begins.
    r = eng.manage(_candle(120_000, 102, 104.5, 102, 104.0))
    assert r is None
    assert pos.tp2_done and abs(pos.remaining_qty - 2.5) < 1e-9

    # After TP2 the stop trails up to candle2's low (102). A dip to it closes
    # the runner -> finalize whole trade as a win.
    r = eng.manage(_candle(180_000, 103, 103, 101.5, 102.0))
    assert r is not None
    assert r.result == "win"
    # Realized gross = 5*(102-100) + 2.5*(104-100) + 2.5*(102-100) = 25, less fees.
    assert r.pnl_usdt > 0
    assert eng.position is None


def test_single_target_closes_fully_at_tp1():
    eng = _engine()
    intent = TradeIntent("BTCUSDT", "short", "liquidity_sweep_choch",
                         entry_price=100.0, stop_loss=101.0, take_profit_1=95.0,
                         take_profit_2=0.0, confluence_score=8, expected_rr=5.0)
    dec = RiskDecision(True, "ok", position_notional=1000.0, quantity=10.0, leverage=5)
    eng.open(intent, dec, ts=0)
    # Price reaches TP1 => full close, no scaling (tp2 == 0).
    r = eng.manage(_candle(60_000, 100, 100, 94.5, 95.0))
    assert r is not None and r.reason == "take_profit"
    assert eng.position is None
