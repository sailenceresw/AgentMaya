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

    # -- info --
    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]}

    def user_state(self, address):
        if self._pending and self.fill == "later":
            self._apply_fill(*self._pending)
            self._pending = None
        aps = [{"position": {"coin": "BTC", "szi": str(self.size), "entryPx": str(self.entry_px)}}] \
            if self.size else []
        return {"assetPositions": aps, "marginSummary": {"accountValue": "50.0"},
                "withdrawable": "40.0"}

    def frontend_open_orders(self, address):
        return [dict(o, oid=oid) for oid, o in self.orders.items()]

    def user_fills(self, address):
        return list(self.fills)

    def all_mids(self):
        return {"BTC": str(self.mid)}

    # -- exchange --
    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("leverage", leverage, name, is_cross))

    def _next(self):
        self._oid += 1
        return self._oid

    def _apply_fill(self, oid, is_buy, sz, px):
        self.size += sz if is_buy else -sz
        self.entry_px = px
        self.orders.pop(oid, None)
        self.fills.append({"coin": "BTC", "oid": oid, "px": str(px), "sz": str(sz),
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
            self._apply_fill(oid, is_buy, sz, limit_px)
            return {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": str(sz), "avgPx": str(limit_px), "oid": oid}}]}}}
        self.orders[oid] = {"coin": name, "isTrigger": False, "reduceOnly": False,
                            "orderType": "Limit"}
        if self.fill == "later":
            self._pending = (oid, is_buy, sz, limit_px)
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        self.orders.pop(oid, None)

    def market_close(self, coin):
        self.calls.append(("market_close", coin))
        if self.close_fails:
            raise RuntimeError("venue down")
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


def test_close_flattens_and_leaves_no_orders():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    broker.close_position(_pos(), 60200.0, "max_duration")
    assert venue.size == 0 and venue.orders == {}


def test_close_after_the_venue_stop_fired_sends_no_second_close():
    venue = _Venue()
    broker = _broker(venue)
    broker.open_position(_intent(), 0.001, 10)
    venue.size = 0.0                                       # venue trigger already closed it
    venue.calls.clear()
    broker.close_position(_pos(), 59700.0, "stop_loss")
    assert ("market_close", "BTC") not in venue.calls
    assert venue.orders == {}                              # leftover target cancelled


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


def test_diagnostics_report_matches_the_other_preflights():
    venue = _Venue()
    venue.size, venue.entry_px = 0.001, 60000.0
    report = run_diagnostics(_settings(), _broker(venue))
    assert report.ok and report.usdt_balance == 50.0 and report.last_price == 60000.0
    assert report.open_positions == [] and any("without a stop" in w for w in report.warnings)


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
