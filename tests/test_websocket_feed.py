"""Tests for the WebSocket feed's message parsing and candle assembly. These
never open a real connection — parse_kline_event/parse_agg_trade_event are
pure functions, and _handle_message is exercised directly with crafted JSON
frames matching Binance's documented combined-stream format."""
import json

from maya.config import RunMode, Settings
from maya.data.websocket_feed import WebSocketFeed, parse_agg_trade_event, parse_kline_event


def _kline_frame(t: int, o: str, h: str, l: str, c: str, v: str, closed: bool) -> str:
    return json.dumps({
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline", "E": t, "s": "BTCUSDT",
            "k": {"t": t, "T": t + 59_999, "s": "BTCUSDT", "i": "1m",
                  "o": o, "c": c, "h": h, "l": l, "v": v, "n": 100,
                  "x": closed, "q": "0", "V": "0", "Q": "0", "B": "0"},
        },
    })


def _agg_trade_frame(t: int, price: str, qty: str, is_buyer_maker: bool) -> str:
    return json.dumps({
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "E": t, "a": 1, "s": "BTCUSDT",
                 "p": price, "q": qty, "f": 1, "l": 1, "T": t, "m": is_buyer_maker},
    })


def _settings(**kw):
    base = dict(mode=RunMode.TESTNET, symbol="BTCUSDT", timeframe="1m",
               binance_api_key="x", binance_api_secret="y")
    base.update(kw)
    return Settings(**base)


# -- pure parsers -------------------------------------------------------------- #

def test_parse_kline_event_open_kline_not_closed():
    msg = json.loads(_kline_frame(60_000, "100", "105", "99", "104", "50", closed=False))
    candle, is_closed = parse_kline_event(msg)
    assert candle is not None
    assert is_closed is False
    assert candle.open == 100 and candle.close == 104


def test_parse_kline_event_closed_kline():
    msg = json.loads(_kline_frame(60_000, "100", "105", "99", "104", "50", closed=True))
    candle, is_closed = parse_kline_event(msg)
    assert is_closed is True
    assert candle.timestamp == 60_000


def test_parse_agg_trade_event():
    msg = json.loads(_agg_trade_frame(60_500, "101.5", "0.02", True))
    trade = parse_agg_trade_event(msg)
    assert trade is not None
    assert trade.price == 101.5
    assert trade.qty == 0.02
    assert trade.is_buyer_maker is True


def test_parse_agg_trade_event_ignores_non_trade_message():
    msg = json.loads(_kline_frame(60_000, "100", "105", "99", "104", "50", closed=True))
    assert parse_agg_trade_event(msg) is None


def test_parse_kline_event_ignores_non_kline_message():
    msg = json.loads(_agg_trade_frame(60_500, "101.5", "0.02", True))
    candle, closed = parse_kline_event(msg)
    assert candle is None
    assert closed is False


# -- WebSocketFeed message handling / candle assembly -------------------------- #

def test_handle_message_buffers_trades_until_kline_closes():
    feed = WebSocketFeed(_settings())
    assert feed._handle_message(_agg_trade_frame(60_100, "100", "1", False)) is None
    assert feed._handle_message(_agg_trade_frame(60_200, "101", "2", True)) is None
    # kline for the same 1m bucket [60000, 120000) closes now
    candle = feed._handle_message(
        _kline_frame(60_000, "100", "102", "99", "101", "3", closed=True)
    )
    assert candle is not None
    assert len(candle.trades) == 2
    assert {t.price for t in candle.trades} == {100.0, 101.0}


def test_handle_message_ignores_unclosed_kline():
    feed = WebSocketFeed(_settings())
    candle = feed._handle_message(
        _kline_frame(60_000, "100", "102", "99", "101", "3", closed=False)
    )
    assert candle is None


def test_handle_message_drops_stale_trade_buckets_on_close():
    feed = WebSocketFeed(_settings())
    # A trade lands in an old bucket that never gets a corresponding close
    # (e.g. a missed event) — it must not leak forever.
    feed._handle_message(_agg_trade_frame(1_000, "50", "1", False))     # bucket 0
    feed._handle_message(_agg_trade_frame(60_100, "100", "1", False))  # bucket 60000
    feed._handle_message(_kline_frame(60_000, "100", "102", "99", "101", "3", closed=True))
    assert 0 not in feed._trades_buffer
    assert 60_000 not in feed._trades_buffer  # popped when the candle closed


def test_handle_message_returns_none_for_garbage_json():
    feed = WebSocketFeed(_settings())
    assert feed._handle_message("not json") is None


def test_ws_url_uses_testnet_host_for_testnet_mode():
    feed = WebSocketFeed(_settings(mode=RunMode.TESTNET))
    assert "stream.binancefuture.com" in feed._ws_url()
    assert "btcusdt@kline_1m" in feed._ws_url()
    assert "btcusdt@aggtrade" in feed._ws_url().lower()


def test_ws_url_uses_mainnet_host_for_live_mode():
    feed = WebSocketFeed(_settings(mode=RunMode.LIVE, live_confirm="I_UNDERSTAND_THE_RISK"))
    assert "fstream.binance.com" in feed._ws_url()
