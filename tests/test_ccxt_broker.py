"""Binance (ccxt) broker order construction — no network."""
from __future__ import annotations

from maya.config import RunMode, Settings
from maya.execution.brokers import CCXTBroker
from maya.execution.models import Position
from maya.strategy.models import TradeIntent


class _Ex:
    def __init__(self, open_orders=()):
        self.orders = []
        self.cancelled = []
        self._open = list(open_orders)

    def set_margin_mode(self, *a): pass
    def set_leverage(self, *a): pass

    def create_order(self, symbol, type_, side, amount, price, params=None):
        self.orders.append((type_, side, amount, price, dict(params or {})))

    def fetch_open_orders(self, symbol):
        return self._open

    def cancel_order(self, oid, symbol):
        self.cancelled.append(oid)


def _broker(ex) -> CCXTBroker:
    b = CCXTBroker.__new__(CCXTBroker)
    b.settings, b.ex = Settings(mode=RunMode.TESTNET), ex
    return b


def _stops(ex):
    return [o for o in ex.orders if o[0] == "STOP_MARKET"]


def test_entry_stop_closes_the_position_without_reduce_only_or_amount():
    """Binance rejects closePosition with reduceOnly or a quantity (-1106),
    which left every Binance position without its stop-loss."""
    ex = _Ex()
    intent = TradeIntent("BTCUSDT", "long", "x", entry_price=100, stop_loss=99,
                         take_profit_1=105, take_profit_2=106, confluence_score=8,
                         expected_rr=5)
    assert _broker(ex).open_position(intent, 0.5, 5)
    (_, side, amount, _, params), = _stops(ex)
    assert side == "sell" and amount is None
    assert params["closePosition"] is True and "reduceOnly" not in params
    assert params["workingType"] == "MARK_PRICE" and params["stopPrice"] == 99


def test_breakeven_stop_replacement_uses_the_same_valid_params():
    ex = _Ex(open_orders=[{"id": "s1", "type": "STOP_MARKET", "side": "buy"}])
    pos = Position("BTCUSDT", "short", "x", entry_price=100, stop_loss=101, take_profit=95,
                   quantity=0.5, notional=50, leverage=5, opened_ts=0,
                   confluence_score=8, rr_planned=5)
    assert _broker(ex).update_stop(pos, 100.0)
    assert ex.cancelled == ["s1"]
    (_, side, amount, _, params), = _stops(ex)
    assert side == "buy" and amount is None and "reduceOnly" not in params
