"""Offline tests for the ccxt feed/broker plumbing.

A fake exchange stands in for ccxt so we can verify the Binance integration
logic deterministically: kline -> Candle conversion, unified-symbol use, and the
entry/SL/TP order placement, without any network access.
"""
from maya.config import RunMode, Settings
from maya.strategy.models import TradeIntent
from maya.execution.models import Position


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.leverage = None
        self.margin_mode = None

    def load_markets(self):
        return {}

    def fetch_ohlcv(self, symbol, timeframe="1m", limit=2):
        # two rows; the runner uses the second-to-last (closed) candle
        base = 1_700_000_000_000
        return [
            [base, 100.0, 110.0, 95.0, 105.0, 1000.0],
            [base + 60_000, 105.0, 106.0, 104.0, 105.5, 50.0],
        ]

    def fetch_trades(self, symbol, since=None, limit=1000):
        return [
            {"timestamp": since + 10, "price": 101.0, "amount": 2.0, "side": "buy"},
            {"timestamp": since + 20, "price": 102.0, "amount": 1.0, "side": "sell"},
        ]

    def amount_to_precision(self, symbol, qty):
        return round(qty, 3)

    def price_to_precision(self, symbol, px):
        return round(px, 2)

    def set_leverage(self, lev, symbol):
        self.leverage = lev

    def set_margin_mode(self, mode, symbol):
        self.margin_mode = mode

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.orders.append({
            "symbol": symbol, "type": type_, "side": side,
            "amount": amount, "price": price, "params": params or {},
        })
        return {"id": str(len(self.orders))}


def _patch(monkeypatch, fake):
    monkeypatch.setattr("maya.data.binance_client.build_exchange", lambda s: fake)


def test_ccxt_feed_converts_klines_to_candles(monkeypatch):
    fake = FakeExchange()
    _patch(monkeypatch, fake)
    from maya.data.ingestion import CCXTFeed

    settings = Settings(mode=RunMode.PAPER, symbol="BTCUSDT", max_candles=1)
    feed = CCXTFeed(settings)
    assert feed.symbol == "BTC/USDT:USDT"
    candles = list(feed.candles())
    assert len(candles) == 1
    c = candles[0]
    assert (c.open, c.high, c.low, c.close, c.volume) == (100.0, 110.0, 95.0, 105.0, 1000.0)


def test_ccxt_broker_places_entry_sl_tp(monkeypatch):
    fake = FakeExchange()
    _patch(monkeypatch, fake)
    from maya.execution.brokers import CCXTBroker

    settings = Settings(mode=RunMode.TESTNET, symbol="BTCUSDT")
    broker = CCXTBroker(settings)
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption",
                         entry_price=100.0, stop_loss=95.0, take_profit_1=120.0,
                         take_profit_2=125.0, confluence_score=8, expected_rr=5.0)
    assert broker.open_position(intent, qty=0.1234, leverage=5) is True

    assert fake.leverage == 5
    assert fake.margin_mode == "ISOLATED"
    types = [o["type"] for o in fake.orders]
    assert types == ["limit", "STOP_MARKET", "limit"]   # entry, SL, TP

    entry, sl, tp = fake.orders
    assert entry["side"] == "buy" and entry["amount"] == 0.123     # rounded
    assert sl["side"] == "sell" and sl["params"]["closePosition"] is True
    assert sl["params"]["workingType"] == "MARK_PRICE"
    # closePosition=True requires NO amount, else Binance rejects with -1106.
    assert sl["amount"] is None
    assert tp["side"] == "sell" and tp["params"]["reduceOnly"] is True


def test_ccxt_broker_market_close_is_reduce_only(monkeypatch):
    fake = FakeExchange()
    _patch(monkeypatch, fake)
    from maya.execution.brokers import CCXTBroker

    broker = CCXTBroker(Settings(mode=RunMode.TESTNET, symbol="BTCUSDT"))
    pos = Position("BTCUSDT", "long", "x", 100, 95, 120, quantity=0.2,
                   notional=20, leverage=5, opened_ts=0, confluence_score=8,
                   rr_planned=5.0)
    broker.close_position(pos, price=120.0, reason="take_profit")
    assert fake.orders[-1]["type"] == "market"
    assert fake.orders[-1]["side"] == "sell"
    assert fake.orders[-1]["params"]["reduceOnly"] is True
