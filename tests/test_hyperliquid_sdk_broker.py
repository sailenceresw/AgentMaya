"""Hyperliquid SDK broker against a fake venue. No network, no SDK import.

``_Venue`` is an INVENTED stand-in. Its response shapes follow the examples
shipped with hyperliquid-python-sdk 0.24.0 (``{"status": "ok", "response":
{"data": {"statuses": [...]}}}``); they were not captured from a live venue.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from maya.check_hyperliquid import run_diagnostics, smoke_test
from maya.config import RunMode, Settings, Venue
from maya.execution.brokers import build_broker
from maya.execution.hyperliquid_broker import (
    MAINNET_URL,
    TESTNET_URL,
    Clients,
    HyperliquidSDKBroker,
    base_url,
    connect,
    round_price,
    round_size,
)
from maya.execution.models import Position
from maya.fees import FeeModel
from maya.strategy.models import TradeIntent

ADDR = "0x" + "ab" * 20


def _settings(**kw):
    kw.setdefault("mode", RunMode.TESTNET)
    return Settings(venue=Venue.HYPERLIQUID, hl_broker="sdk", symbol="BTCUSDT",
                    hl_api_wallet_key="0x" + "11" * 32, hl_account_address=ADDR,
                    hl_entry_timeout_s=3, **kw)


class _Venue:
    """Fake exchange + info. ``fill`` decides what a limit entry does:
    'now' fills at once, 'later' rests then fills on the first poll,
    'never' rests forever."""

    def __init__(self, fill="now", reject_stop=False, mid=60000.0):
        self.fill, self.reject_stop, self.mid = fill, reject_stop, mid
        self.size = 0.0
        self.entry_px = 0.0
        self.orders: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.calls: list[tuple] = []
        self._oid = 100
        self._pending: tuple | None = None
        self.close_fails = False
        self.close_refusal: str | None = None
        self.pos_coin = "BTC"            # the one coin the fake holds a position in
        self.depth = 1e9                 # size resting at the mid on each side

    # -- info --
    def l2_snapshot(self, name):
        lv = [{"px": str(self.mid), "sz": str(self.depth), "n": 1}]
        return {"coin": name, "levels": [lv, lv]}

    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]}

    def user_state(self, address):
        if self._pending and self.fill == "later":
            self._apply_fill(*self._pending)
            self._pending = None
        aps = [{"position": {"coin": self.pos_coin, "szi": str(self.size),
                             "entryPx": str(self.entry_px)}}] if self.size else []
        return {"assetPositions": aps, "marginSummary": {"accountValue": "50.0"},
                "withdrawable": "40.0"}

    def frontend_open_orders(self, address):
        return [dict(o, oid=oid) for oid, o in self.orders.items()]

    def user_fills(self, address):
        return list(self.fills)

    def all_mids(self):
        return {"BTC": str(self.mid), "ETH": str(self.mid)}

    # -- exchange --
    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("leverage", leverage, name, is_cross))

    def _next(self):
        self._oid += 1
        return self._oid

    def _apply_fill(self, oid, is_buy, sz, px, coin="BTC"):
        self.pos_coin = coin
        self.size += sz if is_buy else -sz
        self.entry_px = px
        self.orders.pop(oid, None)
        self.fills.append({"coin": coin, "oid": oid, "px": str(px), "sz": str(sz),
                           "fee": str(px * sz * 0.00015)})

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type, reduce_only))
        oid = self._next()
        if "trigger" in order_type:
            tpsl = order_type["trigger"]["tpsl"]
            if tpsl == "sl" and self.reject_stop:
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "bad px"}]}}}
            self.orders[oid] = {"coin": name, "isTrigger": True, "reduceOnly": reduce_only,
                                "orderType": "Stop Market" if tpsl == "sl" else "Take Profit Market",
                                "triggerPx": str(order_type["trigger"]["triggerPx"])}
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}
        if self.fill == "now":
            self._apply_fill(oid, is_buy, sz, limit_px, name)
            return {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": str(sz), "avgPx": str(limit_px), "oid": oid}}]}}}
        self.orders[oid] = {"coin": name, "isTrigger": False, "reduceOnly": False,
                            "orderType": "Limit"}
        if self.fill == "later":
            self._pending = (oid, is_buy, sz, limit_px, name)
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        self.orders.pop(oid, None)

    def market_close(self, coin):
        self.calls.append(("market_close", coin))
        if self.close_fails:
            raise RuntimeError("venue down")
        if self.close_refusal:           # the SDK returns a refusal, it does not raise
            return {"status": "err", "response": self.close_refusal}
        if coin != self.pos_coin:        # the SDK closes only the coin it is given
            return None
        if self.size:
            self.fills.append({"coin": coin, "oid": self._next(), "px": str(self.mid),
                               "sz": str(abs(self.size)), "fee": "0.001"})
        self.size = 0.0


def _broker(venue, **kw):
    ticks = iter(range(10_000))
    return HyperliquidSDKBroker(_settings(**kw), Clients(venue, venue),
                                sleep=lambda s: None, clock=lambda: float(next(ticks)))


def _intent(direction="long"):
    if direction == "long":
        return TradeIntent("BTCUSDT", "long", "s", 60000, 59700, 61500, 61500, 8, expected_rr=5.0)
    return TradeIntent("BTCUSDT", "short", "s", 60000, 60300, 58500, 58500, 8, expected_rr=5.0)


def _stops(venue):
    return [o for o in venue.orders.values() if o["orderType"] == "Stop Market"]


# -- network selection ------------------------------------------------------ #
def test_testnet_is_the_default():
    assert base_url(_settings()) == TESTNET_URL


@pytest.mark.parametrize("kw", [
    dict(mode=RunMode.LIVE, live_confirm="I_UNDERSTAND_THE_RISK", hl_mainnet=True),
    dict(mode=RunMode.LIVE, live_confirm="I_UNDERSTAND_THE_RISK",
         hl_mainnet_confirm="I_UNDERSTAND_THE_RISK"),
    dict(mode=RunMode.LIVE, hl_mainnet=True, hl_mainnet_confirm="I_UNDERSTAND_THE_RISK"),
    dict(mode=RunMode.TESTNET, hl_mainnet=True, hl_mainnet_confirm="I_UNDERSTAND_THE_RISK"),
])
def test_mainnet_is_refused_unless_every_flag_is_set(kw):
    assert base_url(_settings(**kw)) == TESTNET_URL


def test_mainnet_needs_live_mode_the_interlock_and_both_flags():
    s = _settings(mode=RunMode.LIVE, live_confirm="I_UNDERSTAND_THE_RISK",
                  hl_mainnet=True, hl_mainnet_confirm="I_UNDERSTAND_THE_RISK")
    assert base_url(s) == MAINNET_URL


def test_broker_selection_and_settings():
    assert _settings().uses_hl_sdk and not _settings().uses_metamask
    mm = Settings(venue=Venue.HYPERLIQUID, mode=RunMode.TESTNET)
    assert mm.uses_metamask and not mm.uses_hl_sdk          # mm stays the default
    assert not _settings(mode=RunMode.SIM).uses_hl_sdk      # sim never places orders
    with pytest.raises(ValidationError):
        Settings(hl_broker="rest")


def test_connect_refuses_without_the_api_wallet_and_never_shows_the_key():
    with pytest.raises(RuntimeError, match="HL_API_WALLET_KEY"):
        connect(Settings(venue=Venue.HYPERLIQUID, hl_broker="sdk", mode=RunMode.TESTNET))
    assert "11" * 32 not in repr(_settings())


def test_sdk_path_carries_no_metamask_builder_fee():
    assert FeeModel.for_settings(_settings()).builder == 0.0
    assert FeeModel.for_settings(Settings(venue=Venue.HYPERLIQUID)).builder > 0


def test_build_broker_returns_the_sdk_broker(monkeypatch):
    venue = _Venue()
    monkeypatch.setattr("maya.execution.hyperliquid_broker.connect",
                        lambda settings: Clients(venue, venue))
    assert isinstance(build_broker(_settings()), HyperliquidSDKBroker)


# -- rounding --------------------------------------------------------------- #
def test_price_and_size_rounding_follow_the_venue_rules():
    assert round_price(60123.456, 5) == 60123.0             # 5 significant figures
    assert round_price(1.234567, 2) == 1.2346               # at most 6 - szDecimals decimals
    assert round_size(0.000199, 5) == 0.00019               # down, never up


# -- entries ---------------------------------------------------------------- #
def test_entry_sets_isolated_leverage_then_places_stop_and_target():
    venue = _Venue()
    broker = _broker(venue)
    assert broker.open_position(_intent(), 0.001, 10) is True
    assert venue.calls[0] == ("leverage", 10, "BTC", False)          # isolated
    entry = venue.calls[1]
    assert entry[1:5] == ("BTC", True, 0.001, 60000.0) and entry[5] == {"limit": {"tif": "Gtc"}}
    sl, tp = venue.calls[2], venue.calls[3]
    assert sl[2] is False and sl[6] is True                          # sell, reduce-only
    assert sl[5] == {"trigger": {"triggerPx": 59700.0, "isMarket": True, "tpsl": "sl"}}
    assert tp[5]["trigger"]["tpsl"] == "tp" and tp[5]["trigger"]["triggerPx"] == 61500.0
    assert broker.last_fill.price == 60000.0 and broker.last_fill.size == 0.001
    assert len(_stops(venue)) == 1


def test_short_entry_mirrors_the_sides():
    venue = _Venue()
    assert _broker(venue).open_position(_intent("short"), 0.001, 10)
    assert venue.calls[1][2] is False and venue.calls[2][2] is True  # sell entry, buy stop
    assert venue.size == -0.001


def test_order_under_ten_dollars_is_not_sent():
    venue = _Venue()
    assert _broker(venue).open_position(_intent(), 0.0001, 10) is False   # $6
    assert venue.calls == []


def test_resting_entry_that_fills_while_waiting_gets_its_stop():
    venue = _Venue(fill="later")
    assert _broker(venue).open_position(_intent(), 0.001, 10) is True
    assert len(_stops(venue)) == 1


def test_resting_entry_that_never_fills_is_cancelled_not_left_on_the_book():
    venue = _Venue(fill="never")
    assert _broker(venue).open_position(_intent(), 0.001, 10) is False
    assert venue.orders == {} and venue.size == 0
    assert any(c[0] == "cancel" for c in venue.calls)


# -- testnet price map ------------------------------------------------------ #
class _Real:
    """The real market's info client: only ``all_mids`` is read."""

    def __init__(self, mid=None):
        self.mid = mid

    def all_mids(self):
        if self.mid is None:
            raise ConnectionError("mainnet unreachable")
        return {"BTC": str(self.mid)}


def _mapped(venue, real, **kw):
    ticks = iter(range(10_000))
    return HyperliquidSDKBroker(_settings(**kw), Clients(venue, venue, reference=real),
                                sleep=lambda s: None, clock=lambda: float(next(ticks)))


def test_testnet_prices_are_scaled_and_the_fill_is_reported_unscaled():
    """Regression: testnet BTC traded about 0.8% under the real price the
    signals come from, so an unscaled long put its stop above the market."""
    venue = _Venue(mid=59400.0)                       # testnet 1% under the real 60000
    broker = _mapped(venue, _Real(60000.0))
    assert broker.open_position(_intent(), 0.001, 10) is True
    assert venue.calls[1][4] == 59400.0                               # entry 60000 * 0.99
    assert venue.calls[2][5]["trigger"]["triggerPx"] == 59103.0       # stop 59700 * 0.99
    assert venue.calls[3][5]["trigger"]["triggerPx"] == 60885.0       # target 61500 * 0.99
    assert broker.last_fill.price == pytest.approx(60000.0)           # engine's scale
    assert broker.update_stop(_pos(), 60050.0) is True
    assert _stops(venue)[-1]["triggerPx"] == str(round_price(60050.0 * 0.99, 5))


def test_no_entry_when_the_real_price_cannot_be_read():
    venue = _Venue(mid=59400.0)
    assert _mapped(venue, _Real(None)).open_position(_intent(), 0.001, 10) is False
    assert venue.calls == []


def test_no_entry_when_testnet_is_too_far_from_the_real_price():
    venue = _Venue(mid=30000.0)
    assert _mapped(venue, _Real(60000.0)).open_position(_intent(), 0.001, 10) is False
    assert venue.calls == []


def test_only_a_testnet_connection_gets_the_real_market_reference(monkeypatch):
    import sys
    import types

    class _Info:
        def __init__(self, url, skip_ws=False):
            self.url = url

    fake_info, fake_ex = types.ModuleType("hyperliquid.info"), types.ModuleType("hyperliquid.exchange")
    fake_info.Info = _Info
    fake_ex.Exchange = lambda wallet, url, account_address=None: object()
    monkeypatch.setitem(sys.modules, "hyperliquid.info", fake_info)
    monkeypatch.setitem(sys.modules, "hyperliquid.exchange", fake_ex)

    assert connect(_settings()).reference.url == MAINNET_URL
    assert connect(_settings(hl_testnet_price_map=False)).reference is None
    live = _settings(mode=RunMode.LIVE, live_confirm="I_UNDERSTAND_THE_RISK",
                     hl_mainnet=True, hl_mainnet_confirm="I_UNDERSTAND_THE_RISK")
    assert connect(live).reference is None            # mainnet prices are never scaled


def test_market_entry_is_an_ioc_limit_through_the_book():
    venue = _Venue(mid=60000.0)
    broker = _broker(venue, perps_order_type="market", perps_max_slippage_bps=50)
    assert broker.open_position(_intent(), 0.001, 10) is True
    entry = venue.calls[1]
    assert entry[4] == 60300.0 and entry[5] == {"limit": {"tif": "Ioc"}}   # mid + 0.5%
    assert len(_stops(venue)) == 1


def test_position_is_closed_at_once_if_the_stop_cannot_be_placed():
    venue = _Venue(reject_stop=True)
    assert _broker(venue).open_position(_intent(), 0.001, 10) is False
    assert venue.size == 0 and ("market_close", "BTC") in venue.calls


def test_rejected_entry_places_nothing_else():
    venue = _Venue()
    venue.order = lambda *a, **k: {"status": "ok", "response": {"data": {"statuses": [
        {"error": "Insufficient margin"}]}}}
    assert _broker(venue).open_position(_intent(), 0.001, 10) is False
    assert venue.size == 0


# -- stop updates and exits ------------------------------------------------- #
def _pos():
    return Position("BTCUSDT", "long", "s", 60000, 59700, 61500, 0.001, 60, 10, 0, 8, 5.0)


def test_stop_update_places_the_new_stop_before_cancelling_the_old():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    old = broker._stop_oid
    venue.calls.clear()
    assert broker.update_stop(_pos(), 60050.0) is True
    assert [c[0] for c in venue.calls] == ["order", "cancel"]
    assert venue.calls[1] == ("cancel", "BTC", old)
    assert [o["triggerPx"] for o in _stops(venue)] == ["60050.0"]


def test_failed_stop_update_keeps_the_old_stop():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    venue.reject_stop = True
    assert broker.update_stop(_pos(), 60050.0) is False
    assert [o["triggerPx"] for o in _stops(venue)] == ["59700.0"]


def test_orders_follow_the_trade_symbol_not_the_configured_one():
    """Regression: the coin was fixed from MAYA_SYMBOL (BTC) at start, so the
    multi-coin scanner would have sent an ETH signal as a BTC order at ETH's
    prices. Entry, stop, target, stop move and close must all be on ETH."""
    venue = _Venue(mid=3000.0)
    broker = _broker(venue)                                    # configured for BTCUSDT
    eth = TradeIntent("ETHUSDT", "long", "s", 3000, 2985, 3075, 3075, 8, expected_rr=5.0)
    assert broker.open_position(eth, 0.01, 10) is True
    pos = Position("ETHUSDT", "long", "s", 3000, 2985, 3075, 0.01, 30, 10, 0, 8, 5.0)
    assert broker.update_stop(pos, 3001.0) is True
    broker.close_position(pos, 3010.0, "max_duration")
    names = [c[2] if c[0] == "leverage" else c[1] for c in venue.calls]
    assert names and set(names) == {"ETH"}
    assert venue.size == 0 and venue.orders == {}


def test_a_coin_the_venue_does_not_list_is_not_traded():
    venue = _Venue()
    intent = TradeIntent("NOPEUSDT", "long", "s", 1.0, 0.99, 1.05, 1.05, 8, expected_rr=5.0)
    assert _broker(venue).open_position(intent, 100, 10) is False
    assert venue.calls == []


def test_close_flattens_and_leaves_no_orders():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    broker.close_position(_pos(), 60200.0, "max_duration")
    assert venue.size == 0 and venue.orders == {}


def test_orders_a_refused_cancel_leaves_behind_are_reported():
    """Regression (safety): cancels were assumed to work. A reduce-only stop or
    target left open can close the next position in the same coin unasked."""
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    venue.cancel = lambda name, oid: {"status": "err", "response": "API wallet does not exist"}
    broker.close_position(_pos(), 60200.0, "max_duration")
    assert venue.size == 0 and sorted(broker.leftover_orders) == sorted(venue.orders)
    assert len(broker.leftover_orders) == 2                # the stop and the target


def test_a_clean_close_reports_no_leftovers():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    broker.close_position(_pos(), 60200.0, "max_duration")
    assert broker.leftover_orders == [] and venue.orders == {}


def test_close_after_the_venue_stop_fired_sends_no_second_close():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    venue.size = 0.0                                       # venue trigger already closed it
    venue.calls.clear()
    broker.close_position(_pos(), 59700.0, "stop_loss")
    assert ("market_close", "BTC") not in venue.calls
    assert venue.orders == {}                              # leftover target cancelled


def test_a_refused_close_keeps_the_stop_and_target_and_says_so():
    """Regression (testnet, 20 Nov 2026): HYPE's close was refused, both
    venue orders were cancelled anyway, and the bare short was liquidated."""
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    protective = dict(venue.orders)
    venue.close_refusal = "Order could not immediately match against any resting orders."
    assert broker.close_position(_pos(), 60200.0, "stop_loss") is False
    assert venue.size == 0.001 and venue.orders == protective     # both still resting
    assert not any(c[0] == "cancel" for c in venue.calls)


def test_a_clean_close_returns_true():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    assert broker.close_position(_pos(), 60200.0, "max_duration") is True


def test_exit_is_read_from_this_trades_fills_not_the_newest_for_the_coin():
    """Regression: the newest fill for the coin was logged as the exit. After a
    refused close that was the entry; on PUMP it was an older trade's exit."""
    venue = _Venue()
    broker = _broker(venue)
    venue.fills.append({"coin": "BTC", "oid": 1, "px": "55555", "sz": "9", "fee": "0"})  # old trade
    broker.open_position(_intent(), 0.001, 10)
    entry_oid = venue.fills[-1]["oid"]
    seen = {}
    broker._log_fill = lambda what, oid, px, started, fills=None: seen.update(fills=fills)
    venue.size = 0.0                     # the venue stop fired; its fill is not listed yet
    broker.close_position(_pos(), 59700.0, "stop_loss")
    assert seen["fills"] == []           # nothing logged as the exit, rather than a wrong one
    # Once the stop's own fill is there, that is the exit.
    broker.open_position(_intent(), 0.001, 10)
    stop_oid = broker._stop_oid
    venue.fills.append({"coin": "BTC", "oid": stop_oid, "px": "59700", "sz": "0.001", "fee": "0"})
    venue.size = 0.0
    broker.close_position(_pos(), 59700.0, "stop_loss")
    assert [f["oid"] for f in seen["fills"]] == [stop_oid]
    assert entry_oid not in [f["oid"] for f in seen["fills"]]


def test_no_entry_while_the_venue_already_holds_the_coin():
    """Regression (testnet, 20 Nov 2026): a second HYPE short was sent while
    the first was stuck open, and the venue added the two together."""
    venue = _Venue()
    venue.size, venue.entry_px = -3.25, 60000.0
    broker = _broker(venue)
    venue.calls.clear()
    assert broker.open_position(_intent(), 0.001, 10) is False
    assert not any(c[0] == "order" for c in venue.calls) and venue.size == -3.25


def test_no_entry_into_a_book_too_thin_to_close():
    venue = _Venue()
    venue.depth = 0.0005                 # less than the 0.001 order on each side
    broker = _broker(venue)
    venue.calls.clear()
    assert broker.open_position(_intent(), 0.001, 10) is False
    assert not any(c[0] == "order" for c in venue.calls) and venue.size == 0
    venue.depth = 0.01
    assert broker.open_position(_intent(), 0.001, 10) is True


def test_engine_keeps_the_position_when_the_venue_refuses_the_close():
    """Regression (testnet, 20 Nov 2026): the engine journaled a refused close
    as a win and went flat, so the next signal opened on top of it."""
    from maya.config import RunMode as RM
    from maya.data.models import Candle
    from maya.execution.engine import ExecutionEngine

    eng = ExecutionEngine(Settings(mode=RM.SIM))

    class Refusing:
        def close_position(self, pos, price, reason):
            return False
    eng.broker = Refusing()
    eng.position = _pos()
    stop_hit = Candle(timestamp=60_000, open=59600, high=59650, low=59000, close=59100,
                      volume=1, taker_buy_volume=0.5)
    assert eng.manage(stop_hit) is None
    assert eng.position is not None and eng.busy


# -- exits booked at the venue's fill --------------------------------------- #
def _stop_fill(venue, broker, px, fee="0.02"):
    """The venue's stop trigger fires at ``px`` (invented numbers)."""
    venue.fills.append({"coin": "BTC", "oid": broker._stop_oid, "px": str(px),
                        "sz": str(abs(venue.size)), "fee": fee, "time": 1})
    venue.orders.pop(broker._stop_oid, None)
    venue.size = 0.0


def test_close_reports_the_venue_exit_fill_and_which_trigger_filled():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    _stop_fill(venue, broker, 59500.0)                 # slipped 200 under the 59700 stop
    assert broker.close_position(_pos(), 59700.0, "take_profit") is True
    assert broker.last_exit.price == 59500.0 and broker.last_exit.size == 0.001
    assert broker.last_exit.fee == pytest.approx(0.02)
    assert broker.last_exit.reason == "stop_loss"      # the venue's trigger, not the guess


def test_venue_exit_is_reported_on_the_signal_scale():
    venue = _Venue(mid=59400.0)                        # testnet 1% under the real 60000
    broker = _mapped(venue, _Real(60000.0))
    broker.open_position(_intent(), 0.001, 10)
    _stop_fill(venue, broker, 59004.0)
    broker.close_position(_pos(), 59700.0, "stop_loss")
    assert broker.last_exit.price == pytest.approx(59600.0)     # 59004 / 0.99


def test_entry_fee_is_read_from_the_entry_fill():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    assert broker.last_fill.fee == pytest.approx(60000.0 * 0.001 * 0.00015)


def test_no_fill_record_waits_briefly_then_reports_none():
    venue = _Venue()
    naps: list[float] = []
    ticks = iter(range(10_000))
    broker = HyperliquidSDKBroker(_settings(), Clients(venue, venue), sleep=naps.append,
                                  clock=lambda: float(next(ticks)))
    broker.open_position(_intent(), 0.001, 10)
    venue.size = 0.0                                   # flat, but no fill listed
    assert broker.close_position(_pos(), 59700.0, "stop_loss") is True
    assert broker.last_exit is None and naps == [1.0, 1.0, 1.0]


def test_a_fill_listed_a_moment_late_is_still_used():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    stop_oid = broker._stop_oid
    venue.size = 0.0
    reads = {"n": 0}
    real = venue.user_fills

    def late(address):
        reads["n"] += 1
        if reads["n"] == 3:                            # appears on the second exit read
            venue.fills.append({"coin": "BTC", "oid": stop_oid, "px": "59650",
                                "sz": "0.001", "fee": "0.01", "time": 2})
        return real(address)
    venue.user_fills = late
    broker.close_position(_pos(), 59700.0, "stop_loss")
    assert broker.last_exit is not None and broker.last_exit.price == 59650.0


class _Decision:
    approved = True
    quantity = 0.001
    position_notional = 60.0
    leverage = 10


def _engine_on(venue):
    from maya.execution.engine import ExecutionEngine

    eng = ExecutionEngine(Settings(mode=RunMode.SIM))
    eng.broker = _broker(venue)
    assert eng.open(_intent(), _Decision(), ts=0) is True
    return eng


def test_engine_books_the_venue_exit_price_and_fees_not_the_planned_ones():
    """Regression (testnet, Nov 2026): the journal booked the planned stop
    and target, so it showed a profit the venue never paid, and a PUMP stop
    that slipped 38 bps looked exact."""
    from maya.data.models import Candle

    venue = _Venue()
    eng = _engine_on(venue)
    entry_fee = eng.position.entry_fee_usd
    assert entry_fee == pytest.approx(0.009)
    _stop_fill(venue, eng.broker, 59500.0, fee="0.02")
    stop_hit = Candle(timestamp=60_000, open=59800, high=59800, low=59600, close=59650,
                      volume=1, taker_buy_volume=0.5)
    result = eng.manage(stop_hit)
    assert result.exit_price == 59500.0                         # not the planned 59700
    assert result.fees_usdt == pytest.approx(entry_fee + 0.02)  # the venue's, not the model's
    assert result.pnl_usdt == pytest.approx((59500.0 - 60000.0) * 0.001 - entry_fee - 0.02)


def test_engine_falls_back_to_the_planned_price_without_a_fill_record():
    from maya.data.models import Candle

    venue = _Venue()
    eng = _engine_on(venue)
    venue.size = 0.0                                            # flat, no fill listed
    stop_hit = Candle(timestamp=60_000, open=59800, high=59800, low=59600, close=59650,
                      volume=1, taker_buy_volume=0.5)
    assert eng.manage(stop_hit).exit_price == 59700.0


# -- position monitor on the SDK path --------------------------------------- #
class _Journal:
    def __init__(self):
        self.rows = []

    def record(self, result, confluences):
        self.rows.append(result)


class _Risk:
    def __init__(self):
        self.closed = []

    def on_trade_closed(self, pnl, ts):
        self.closed.append(pnl)


def _monitored(venue):
    from maya.execution.monitor import PositionMonitor

    eng = _engine_on(venue)
    journal, risk = _Journal(), _Risk()
    return eng, PositionMonitor(_settings(), eng, journal, risk), journal, risk


def test_monitor_books_a_venue_stop_no_candle_touched():
    """Signals use real-market candles; the stop rests on the testnet book.
    A venue stop that fired with no candle touching it left the engine in
    the trade until the hold limit. The monitor now books it at the fill."""
    venue = _Venue()
    eng, monitor, journal, risk = _monitored(venue)
    _stop_fill(venue, eng.broker, 59550.0)
    monitor._poll_once()
    assert eng.position is not None and journal.rows == []      # one flat read is not enough
    monitor._poll_once()
    assert eng.position is None and venue.orders == {}          # target cancelled
    assert len(journal.rows) == 1 and journal.rows[0].exit_price == 59550.0
    assert journal.rows[0].reason == "stop_loss"
    assert risk.closed == [journal.rows[0].pnl_usdt]


def test_monitor_reports_a_venue_target_as_a_take_profit():
    venue = _Venue()
    eng, monitor, journal, _ = _monitored(venue)
    tp_oid = eng.broker._tp_oid
    venue.fills.append({"coin": "BTC", "oid": tp_oid, "px": "61500", "sz": "0.001",
                        "fee": "0.01", "time": 1})
    venue.size = 0.0
    monitor._poll_once()
    monitor._poll_once()
    assert journal.rows[0].reason == "take_profit" and journal.rows[0].result == "win"


def test_monitor_never_books_on_an_unreadable_or_open_venue():
    venue = _Venue()
    eng, monitor, journal, _ = _monitored(venue)
    monitor._poll_once()
    monitor._poll_once()                                        # still open at the venue
    assert eng.position is not None and journal.rows == []
    venue.size = 0.0
    monitor._poll_once()                                        # flat once...
    real = venue.user_state
    venue.user_state = lambda address: (_ for _ in ()).throw(ConnectionError("timeout"))
    monitor._poll_once()                                        # ...then unreadable: resets
    venue.user_state = real
    monitor._poll_once()                                        # flat once again
    assert eng.position is not None and journal.rows == []
    monitor._poll_once()
    assert eng.position is None and len(journal.rows) == 1


# -- startup safety --------------------------------------------------------- #
def test_startup_closes_a_position_that_has_no_stop():
    venue = _Venue()
    venue.size, venue.entry_px = 0.001, 60000.0            # left behind by a crash
    assert _broker(venue).close_unprotected_positions() == ["BTC"]
    assert venue.size == 0


def test_startup_leaves_a_position_that_has_its_stop():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    assert broker.close_unprotected_positions() == []
    assert venue.size == 0.001


REFUSED = "User or API Wallet 0x09c7 does not exist."     # text the testnet returned


def test_a_refused_startup_close_is_not_reported_as_closed():
    """Regression (testnet, Nov 2026): the venue refused the close because the
    API wallet was not approved, the SDK returned that without raising, and
    the preflight logged the position as closed while it stayed open."""
    venue = _Venue()
    venue.size, venue.entry_px = 0.02187, 63969.7
    venue.close_refusal = REFUSED
    broker = _broker(venue)
    assert broker.close_unprotected_positions() == []
    assert broker.unclosed == {"BTC": REFUSED} and venue.size == 0.02187
    report = run_diagnostics(_settings(), broker)
    assert not report.ok and "STILL OPEN" in report.errors[0] and "does not exist" in report.errors[0]
    assert not any("closed position(s)" in w for w in report.warnings)


def test_flatten_reports_the_venue_refusal():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    venue.close_refusal = REFUSED
    assert broker._flatten() is False and venue.size == 0.001


class _Wallet:
    def __init__(self, address):
        self.address = address


def test_preflight_refuses_an_api_wallet_the_venue_does_not_know():
    """Testnet (Nov 2026) answered userRole with {"role": "missing"} for the
    configured API wallet; reads worked, orders and closes did not."""
    venue = _Venue()
    venue.wallet = _Wallet("0x" + "09" * 20)
    venue.user_role = lambda address: {"role": "missing"}
    report = run_diagnostics(_settings(), _broker(venue))
    assert not report.ok and "not approved" in report.errors[0]
    assert "app.hyperliquid-testnet.xyz/API" in report.errors[0]
    assert "put its key in HL_API_WALLET_KEY" in report.errors[0]   # the name config reads


def test_preflight_accepts_an_approved_agent_and_the_main_key():
    venue = _Venue()
    venue.wallet = _Wallet("0x" + "09" * 20)
    venue.user_role = lambda address: {"role": "agent", "data": {"user": ADDR.upper()}}
    assert run_diagnostics(_settings(), _broker(venue)).ok
    venue.wallet = _Wallet(ADDR)                        # signing with the account key itself
    venue.user_role = lambda address: pytest.fail("no role query for the main key")
    assert run_diagnostics(_settings(), _broker(venue)).ok


def test_an_agent_approved_for_another_account_is_refused():
    venue = _Venue()
    venue.wallet = _Wallet("0x" + "09" * 20)
    venue.user_role = lambda address: {"role": "agent", "data": {"user": "0x" + "cd" * 20}}
    assert not run_diagnostics(_settings(), _broker(venue)).ok


def test_diagnostics_report_matches_the_other_preflights():
    venue = _Venue()
    venue.size, venue.entry_px = 0.001, 60000.0
    report = run_diagnostics(_settings(), _broker(venue))
    assert report.ok and report.usdt_balance == 50.0 and report.last_price == 60000.0
    assert report.open_positions == [] and any("without a stop" in w for w in report.warnings)


def test_unified_account_collateral_is_read_from_spot_usdc():
    """Regression: a funded unified account reported account value 0, because
    its collateral is spot USDC and only the perp side was read. The shapes
    below are the ones the testnet returned (Nov 2026)."""
    class _Unified(_Venue):
        mode = "unifiedAccount"

        def user_state(self, address):
            return {"assetPositions": [], "marginSummary": {"accountValue": "0.0"},
                    "withdrawable": "0.0"}

        def query_user_abstraction_state(self, address):
            return self.mode

        def spot_user_state(self, address):
            return {"balances": [{"coin": "USDC", "token": 0, "total": "999.0",
                                  "hold": "9.0", "entryNtl": "0.0"}]}

    venue = _Unified()
    report = run_diagnostics(_settings(), _broker(venue))
    assert report.usdt_balance == 999.0 and report.usdt_available == 990.0
    assert not any("account value is 0" in w for w in report.warnings)

    venue.mode = "default"          # spot USDC is not perp collateral here
    assert run_diagnostics(_settings(), _broker(venue)).usdt_balance == 0.0


def test_diagnostics_fail_cleanly_when_the_venue_is_unreachable(monkeypatch):
    def boom(settings):
        raise ConnectionError("timeout")
    monkeypatch.setattr("maya.execution.hyperliquid_broker.connect", boom)
    report = run_diagnostics(_settings())
    assert not report.ok and "could not connect" in report.errors[0]


# -- smoke test ------------------------------------------------------------- #
def test_smoke_places_a_far_limit_cancels_it_and_leaves_nothing():
    venue = _Venue(fill="never")
    ok, message = smoke_test(_broker(venue))
    assert ok, message
    order = next(c for c in venue.calls if c[0] == "order")
    assert order[2] is True and order[4] == 30000.0        # buy at half the mid
    assert order[3] * order[4] >= 10.0                     # at or above the venue minimum
    assert venue.orders == {} and venue.size == 0


def test_smoke_reports_failure_if_the_order_would_fill():
    ok, message = smoke_test(_broker(_Venue(fill="now")))
    assert not ok and "not accepted as resting" in message
