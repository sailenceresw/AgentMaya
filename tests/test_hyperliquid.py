"""Offline tests for the Hyperliquid adapter (feed, broker, rounding, config).

The SDK is never contacted — fakes stand in for Info/Exchange so we verify the
order construction, precision rounding, candle conversion and reconciliation
deterministically.
"""
from maya.config import RunMode, Settings, Venue
from maya.execution.hyperliquid_broker import HyperliquidBroker, round_px
from maya.strategy.models import TradeIntent


class FakeExchange:
    def __init__(self):
        self.orders = []
        self.leverage = None
        self.closed = False

    def update_leverage(self, lev, name, is_cross=True):
        self.leverage = (lev, is_cross)

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False):
        self.orders.append({"name": name, "is_buy": is_buy, "sz": sz,
                            "px": limit_px, "type": order_type, "reduce_only": reduce_only})
        return {"status": "ok"}

    def market_close(self, coin):
        self.closed = True


class FakeInfo:
    def __init__(self, position_qty=0.0, open_orders=0):
        self._pq = position_qty
        self._oo = open_orders

    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 3},
                             {"name": "ETH", "szDecimals": 2}]}

    def all_mids(self):
        return {"BTC": "67500.0"}

    def user_state(self, addr):
        return {"marginSummary": {"accountValue": "1234.5"},
                "assetPositions": [{"position": {"coin": "BTC", "szi": str(self._pq)}}]}

    def open_orders(self, addr):
        return [{"coin": "BTC"} for _ in range(self._oo)]

    def candles_snapshot(self, coin, interval, start, end):
        base = 1_700_000_000_000
        return [
            {"t": base, "o": "100", "h": "110", "l": "95", "c": "105", "v": "1000"},
            {"t": base + 60_000, "o": "105", "h": "106", "l": "104", "c": "105.5", "v": "50"},
        ]


def _patch(monkeypatch, fake_ex=None, fake_info=None):
    fe = fake_ex or FakeExchange()
    fi = fake_info or FakeInfo()
    monkeypatch.setattr("maya.data.hyperliquid_client.build_exchange", lambda s: fe)
    monkeypatch.setattr("maya.data.hyperliquid_client.build_info", lambda s: fi)
    return fe, fi


def _settings():
    s = Settings(venue=Venue.HYPERLIQUID, mode=RunMode.TESTNET, symbol="BTCUSDT")
    s.hyperliquid_account_address = "0x1234567890abcdef1234567890abcdef12345678"
    return s


def test_coin_derivation():
    assert Settings(symbol="BTCUSDT").coin == "BTC"
    assert Settings(symbol="ETHUSDC").coin == "ETH"
    assert Settings(symbol="SOL").coin == "SOL"


def test_round_px_significant_figures():
    assert round_px(67523.45, 3) == 67523.0      # 5 sig figs, integer
    assert round_px(2.345678, 3) == 2.346         # 5 sig figs -> 3 dp
    assert round_px(0, 3) == 0.0


def test_open_position_scales_out(monkeypatch):
    fe, fi = _patch(monkeypatch)
    broker = HyperliquidBroker(_settings())
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption",
                         entry_price=67500.0, stop_loss=67000.0, take_profit_1=68000.0,
                         take_profit_2=68500.0, confluence_score=8, expected_rr=4.0)
    assert broker.open_position(intent, qty=0.123, leverage=5) is True
    assert fe.leverage == (5, False)  # isolated margin
    # entry (buy), SL trigger (sell, reduce-only), TP1 + TP2 triggers (sell, reduce-only)
    assert len(fe.orders) == 4
    entry, sl, tp1, tp2 = fe.orders
    assert entry["is_buy"] is True and entry["reduce_only"] is False
    assert "limit" in entry["type"]
    assert sl["is_buy"] is False and sl["reduce_only"] is True
    assert sl["type"]["trigger"]["tpsl"] == "sl"
    assert tp1["type"]["trigger"]["tpsl"] == "tp" and abs(tp1["sz"] - 0.062) < 2e-3
    assert tp2["type"]["trigger"]["tpsl"] == "tp" and abs(tp2["sz"] - 0.031) < 2e-3


def test_open_position_single_target(monkeypatch):
    fe, fi = _patch(monkeypatch)
    broker = HyperliquidBroker(_settings())
    intent = TradeIntent("BTCUSDT", "short", "liquidity_sweep_choch",
                         entry_price=67500.0, stop_loss=67800.0, take_profit_1=65000.0,
                         take_profit_2=0.0, confluence_score=8, expected_rr=5.0)
    assert broker.open_position(intent, qty=0.2, leverage=5) is True
    assert len(fe.orders) == 3  # entry, SL, one full TP
    assert all(o["is_buy"] is True for o in fe.orders[1:])  # closing side = buy (short)


def test_open_state_reports_position(monkeypatch):
    fe, fi = _patch(monkeypatch, fake_info=FakeInfo(position_qty=0.05, open_orders=2))
    broker = HyperliquidBroker(_settings())
    st = broker.open_state()
    assert abs(st["position_qty"] - 0.05) < 1e-9
    assert st["open_orders"] == 2


def test_hyperliquid_feed_converts_candles(monkeypatch):
    _patch(monkeypatch)
    from maya.data.hyperliquid_feed import HyperliquidFeed
    feed = HyperliquidFeed(Settings(venue=Venue.HYPERLIQUID, mode=RunMode.PAPER,
                                    symbol="BTCUSDT", max_candles=1))
    candles = list(feed.candles())
    assert len(candles) == 1
    c = candles[0]
    assert (c.open, c.high, c.low, c.close, c.volume) == (100.0, 110.0, 95.0, 105.0, 1000.0)
