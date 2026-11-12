"""Tests for the LIVE execution engine — exchange-tracked fills, no simulation.

A mock broker lets us drive the exchange position/realized-PnL deterministically
and assert the engine stays in lockstep with the exchange.
"""
from maya.config import RunMode, Settings, Venue
from maya.data.models import Candle
from maya.execution.live_engine import LiveExecutionEngine, FILL_TIMEOUT_CANDLES
from maya.risk.manager import RiskDecision
from maya.strategy.models import TradeIntent


class MockBroker:
    def __init__(self):
        self.qty = 0.0            # signed exchange position
        self.realized = 0.0
        self.cancelled = 0
        self.closed = 0
        self.opened = False

    def open_position(self, intent, qty, leverage):
        self.opened = True
        return True

    def sync_qty(self):
        return self.qty

    def realized_since(self, ts):
        return self.realized

    def cancel_resting(self):
        self.cancelled += 1

    def close_position(self, pos, price, reason):
        self.closed += 1
        self.qty = 0.0


def _engine(monkeypatch):
    mb = MockBroker()
    monkeypatch.setattr("maya.execution.live_engine.build_broker", lambda s: mb)
    eng = LiveExecutionEngine(Settings(venue=Venue.HYPERLIQUID, mode=RunMode.TESTNET,
                                       symbol="BTCUSDT"))
    return eng, mb


def _intent():
    return TradeIntent("BTCUSDT", "long", "value_area_absorption", 100, 99, 102, 104,
                       confluence_score=8, expected_rr=4.0)


def _dec():
    return RiskDecision(True, "ok", position_notional=1000.0, quantity=10.0, leverage=5)


def _c(ts, close=100.0):
    return Candle(ts, close, close, close, close, 100, [])


def test_entry_fills_then_exchange_closes_in_profit(monkeypatch):
    eng, mb = _engine(monkeypatch)
    assert eng.open(_intent(), _dec(), ts=0) is True
    assert eng.has_position and eng._pending

    # Exchange shows the fill on the next candle.
    mb.qty = 10.0
    assert eng.manage(_c(60_000)) is None
    assert not eng._pending

    # Exchange's TP trigger closes the position in profit.
    mb.qty = 0.0
    mb.realized = 40.0
    r = eng.manage(_c(120_000))
    assert r is not None and r.result == "win" and r.pnl_usdt == 40.0
    assert eng.position is None
    assert mb.cancelled >= 1          # leftover triggers cleaned up


def test_unfilled_entry_is_cancelled(monkeypatch):
    eng, mb = _engine(monkeypatch)
    eng.open(_intent(), _dec(), ts=0)
    # Position never appears on the exchange -> cancelled after the timeout.
    for i in range(FILL_TIMEOUT_CANDLES):
        assert eng.manage(_c((i + 1) * 60_000)) is None
    assert eng.position is None
    assert mb.cancelled >= 1


def test_max_duration_force_closes(monkeypatch):
    eng, mb = _engine(monkeypatch)
    eng.open(_intent(), _dec(), ts=0)
    mb.qty = 10.0
    eng.manage(_c(60_000))           # confirm fill
    mb.realized = -5.0
    # 5 hours later (> MAX_POSITION_DURATION_HOURS=4) -> force close.
    r = eng.manage(_c(5 * 3_600_000))
    assert r is not None and r.reason == "max_duration"
    assert mb.closed == 1 and r.result == "loss"
