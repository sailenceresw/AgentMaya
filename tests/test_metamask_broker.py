"""Tests for the MetaMask Agent Wallet / Hyperliquid broker.

No network and no `mm` process: :func:`subprocess.run` is replaced with a
recorder that replays payloads captured **verbatim** from a live authenticated
Hyperliquid testnet session (CLI v6.0.0, 2026-08-09) — see the ``REAL_*``
fixtures below. They are the actual wire format, not invented shapes, which is
what makes the parsing assertions meaningful: an earlier version of this file
asserted against guessed key names and happily passed while the parser was
returning 0.0 for every balance.

What these still cannot prove is fill behaviour — how a real position reports
once one is open, and what a partial fill looks like — because the test
sub-account has never held a position. The argv itself *is* venue-validated:
`mm perps open --dry-run` with exactly the argv built here returned
``{"status": "prepared", "averagePrice": "66800", "filledSize": "0.00599"}``.
See docs/RUNNING.md for what remains unverified.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from maya.config import RunMode, Settings, Venue
from maya.execution.brokers import PaperBroker, build_broker
from maya.execution.metamask_broker import (
    MMError,
    MetaMaskPerpsBroker,
    _format_size,
    to_hl_symbol,
)
from maya.execution.models import Position
from maya.strategy.models import TradeIntent


@pytest.fixture
def settings() -> Settings:
    return Settings(mode=RunMode.TESTNET, venue=Venue.HYPERLIQUID, symbol="BTCUSDT")


@pytest.fixture
def intent() -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT", direction="short", setup_name="liquidity_sweep_choch",
        entry_price=66800.0, stop_loss=67388.0, take_profit_1=63700.0,
        take_profit_2=62000.0, confluence_score=7, expected_rr=5.2,
    )


@pytest.fixture
def position() -> Position:
    return Position(
        symbol="BTCUSDT", direction="short", setup_name="liquidity_sweep_choch",
        entry_price=66800.0, stop_loss=67388.0, take_profit=63700.0,
        quantity=0.034, notional=2271.2, leverage=5, opened_ts=0,
        confluence_score=7, rr_planned=5.2,
    )


class FakeMM:
    """Records argv and replays queued responses in order."""

    def __init__(self, *responses: dict) -> None:
        self.calls: list[list[str]] = []
        self.responses = list(responses)

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        payload = self.responses.pop(0) if self.responses else {"ok": True, "data": {}}
        if isinstance(payload, BaseException):
            raise payload
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    def command(self, index: int) -> str:
        """The recorded call at ``index`` as a single string, minus the binary."""
        return " ".join(self.calls[index][1:])


def prime_market_cache(broker: MetaMaskPerpsBroker) -> MetaMaskPerpsBroker:
    """Seed the venue-metadata cache so order-construction tests observe only
    the order calls. Mirrors the real steady state: metadata is fetched once
    per symbol per process, never per order."""
    broker._market_cache["BTC"] = REAL_MARKET_BTC
    return broker


@pytest.fixture
def broker(settings, monkeypatch) -> MetaMaskPerpsBroker:
    monkeypatch.setattr(
        "maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")
    return prime_market_cache(MetaMaskPerpsBroker(settings))


@pytest.fixture
def cold_broker(settings, monkeypatch) -> MetaMaskPerpsBroker:
    """Broker with an empty metadata cache, for tests that exercise lookup."""
    monkeypatch.setattr(
        "maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")
    return MetaMaskPerpsBroker(settings)


def install(monkeypatch, fake: FakeMM) -> None:
    monkeypatch.setattr(subprocess, "run", fake)


# -- symbol / size mapping ------------------------------------------------- #
@pytest.mark.parametrize("maya_symbol,expected", [
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("SOLUSDC", "SOL"),
    ("BTC/USDT:USDT", "BTC"),
    ("btcusdt", "BTC"),
    ("BTC", "BTC"),
    ("USDT", "USDT"),  # degenerate: stripping would leave nothing, so keep it
])
def test_symbol_mapping(maya_symbol, expected):
    assert to_hl_symbol(maya_symbol) == expected


def test_size_rounds_down_never_up():
    # Rounding up would push size past the risk-approved quantity.
    assert _format_size(0.0345678, 5) == "0.03456"
    assert _format_size(0.999999, 2) == "0.99"
    assert _format_size(1.0, 5) == "1.00000"


# -- open ------------------------------------------------------------------ #
def test_open_places_limit_entry_then_attaches_sl_tp(broker, intent, monkeypatch):
    fake = FakeMM({"ok": True, "data": {"orderId": 1}}, {"ok": True, "data": {}})
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is True
    assert len(fake.calls) == 2

    opened = fake.command(0)
    assert "perps open" in opened
    assert "--symbol BTC" in opened
    assert "--side short" in opened
    assert "--size 0.03400" in opened
    assert "--leverage 5" in opened
    assert "--type limit --limit-px 66800" in opened
    assert "--network testnet" in opened
    assert "--venue hyperliquid" in opened
    assert "--yes" in opened and "--json" in opened

    modified = fake.command(1)
    assert "perps modify" in modified
    assert "--sl 67388" in modified
    assert "--tp 63700" in modified


def test_open_returns_false_when_entry_rejected(broker, intent, monkeypatch):
    fake = FakeMM({"ok": False, "error": {"message": "insufficient margin"}})
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is False
    assert len(fake.calls) == 1  # never tried to attach SL/TP to a position that isn't there


def test_open_unwinds_position_when_sl_tp_attach_fails(broker, intent, monkeypatch):
    """The critical safety path: `mm perps open` cannot carry SL/TP, so a
    failed follow-up `modify` leaves a live position with no stop. It must be
    closed immediately rather than left on the book."""
    fake = FakeMM(
        {"ok": True, "data": {"orderId": 1}},          # open succeeds
        {"ok": False, "error": "trigger price invalid"},  # SL/TP attach fails
        {"ok": True, "data": {}},                       # emergency close
    )
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is False
    assert len(fake.calls) == 3
    assert "perps close" in fake.command(2)
    assert "--symbol BTC" in fake.command(2)


def test_open_survives_failed_unwind_without_raising(broker, intent, monkeypatch):
    """If even the emergency close fails there is a naked position on the
    venue. That must be logged as critical, not raised into the graph — the
    engine still needs a clean False so it doesn't record a local position."""
    fake = FakeMM(
        {"ok": True, "data": {"orderId": 1}},
        {"ok": False, "error": "trigger price invalid"},
        {"ok": False, "error": "close failed"},
    )
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is False
    assert len(fake.calls) == 3


def test_market_order_type_sends_slippage_cap(settings, intent, monkeypatch):
    monkeypatch.setattr(
        "maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")
    settings = settings.model_copy(update={"perps_order_type": "market"})
    broker = prime_market_cache(MetaMaskPerpsBroker(settings))
    fake = FakeMM({"ok": True, "data": {}}, {"ok": True, "data": {}})
    install(monkeypatch, fake)

    broker.open_position(intent, 0.034, 5)
    opened = fake.command(0)
    assert "--type market" in opened
    assert "--max-slippage-bps 50" in opened
    assert "--limit-px" not in opened


# -- stop management ------------------------------------------------------- #
def test_update_stop_moves_venue_side_stop(broker, position, monkeypatch):
    fake = FakeMM({"ok": True, "data": {}})
    install(monkeypatch, fake)

    assert broker.update_stop(position, 66800.0) is True
    assert "perps modify" in fake.command(0)
    assert "--sl 66800" in fake.command(0)
    assert "--tp" not in fake.command(0)  # breakeven move must not disturb the target


def test_update_stop_returns_false_on_failure(broker, position, monkeypatch):
    """False keeps the engine from updating pos.stop_loss — a local stop that
    disagrees with the venue is worse than not moving it at all."""
    install(monkeypatch, FakeMM({"ok": False, "error": "no open position"}))
    assert broker.update_stop(position, 66800.0) is False


# -- close ----------------------------------------------------------------- #
def test_close_issues_market_close(broker, position, monkeypatch):
    fake = FakeMM({"ok": True, "data": {}})
    install(monkeypatch, fake)

    broker.close_position(position, 63700.0, "take_profit")
    assert "perps close" in fake.command(0)
    assert "--symbol BTC" in fake.command(0)


def test_close_failure_does_not_raise(broker, position, monkeypatch):
    """Closing when the venue already filled the SL/TP is expected; it must
    not propagate out of the engine's close path."""
    install(monkeypatch, FakeMM({"ok": False, "error": "no position"}))
    broker.close_position(position, 63700.0, "stop_loss")  # must not raise


# -- transport / parsing --------------------------------------------------- #
def test_timeout_reports_unknown_order_state(broker, intent, monkeypatch):
    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(MMError, match="UNKNOWN"):
        broker.perps("positions")


def test_non_json_output_raises_mm_error(broker, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "command not found", ""))
    with pytest.raises(MMError, match="non-JSON"):
        broker.perps("positions")


def test_empty_output_raises_mm_error(broker, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "boom"))
    with pytest.raises(MMError, match="no output"):
        broker.perps("positions")


# --------------------------------------------------------------------------- #
# Fixtures captured VERBATIM from a live `mm ... --network testnet --json` run
# against an authenticated session on 2026-08-09, CLI v6.0.0. These are the
# real wire format, not invented shapes — every numeric field is a string.
# --------------------------------------------------------------------------- #
REAL_BALANCE = {
    "venue": "hyperliquid", "totalBalance": "0", "spendableBalance": "0.0",
    "withdrawableBalance": "0.0", "marginUsed": "0", "unrealizedPnl": "0.00",
    "returnOnEquity": "0",
    "subAccounts": {"main": {"totalBalance": "0", "spendableBalance": "0.0",
                             "withdrawableBalance": "0.0"}},
}
REAL_POSITIONS_EMPTY: list = []   # `perps positions` returns a BARE array
REAL_ORDERS_EMPTY: list = []
REAL_MARKET_BTC = {
    "venue": "hyperliquid", "symbol": "BTC", "maxLeverage": 40,
    "sizeDecimals": 5, "markPrice": "64849.0", "oraclePrice": "65044.9",
    "fundingRate": "-0.0000693105", "openInterest": "61.64058",
    "volume24h": "1908026.8980800002",
}
REAL_DRY_RUN_OPEN = {
    "venue": "hyperliquid", "symbol": "BTC", "status": "prepared",
    "averagePrice": "66800", "filledSize": "0.00599",
}


def test_fetch_collateral_reads_the_real_total_balance_field(broker, monkeypatch):
    """Regression guard for a confirmed bug: the parser originally looked for
    accountValue/equity/totalEquity/withdrawable/available/balance, none of
    which the CLI emits. It returned 0.0 unconditionally, which would have
    seeded the risk manager with a $0 account on a funded venue."""
    funded = dict(REAL_BALANCE, totalBalance="1234.56", spendableBalance="1000.0")
    install(monkeypatch, FakeMM({"ok": True, "data": funded}))
    assert broker.fetch_collateral_usd() == pytest.approx(1234.56)


def test_fetch_collateral_prefers_equity_over_free_margin(broker, monkeypatch):
    """spendableBalance is free margin. Seeding equity from it would understate
    the account whenever margin is committed to an open position."""
    with_position = dict(REAL_BALANCE, totalBalance="1000.0",
                         spendableBalance="200.0", marginUsed="800.0")
    install(monkeypatch, FakeMM({"ok": True, "data": with_position}))
    assert broker.fetch_collateral_usd() == pytest.approx(1000.0)


def test_fetch_collateral_handles_the_real_empty_account(broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": REAL_BALANCE}))
    assert broker.fetch_collateral_usd() == 0.0


def test_balance_detail_breaks_out_the_real_fields(broker, monkeypatch):
    funded = dict(REAL_BALANCE, totalBalance="1000.0", spendableBalance="200.0",
                  withdrawableBalance="150.0", marginUsed="800.0",
                  unrealizedPnl="12.50")
    install(monkeypatch, FakeMM({"ok": True, "data": funded}))
    detail = broker.fetch_balance_detail()
    assert detail == pytest.approx({
        "total": 1000.0, "spendable": 200.0, "withdrawable": 150.0,
        "margin_used": 800.0, "unrealized_pnl": 12.5,
    })


def test_fetch_position_parses_the_real_bare_array(broker, monkeypatch):
    """`perps positions` returns a bare JSON array, not a {"positions": [...]}
    object — confirmed as {"ok": true, "data": []}."""
    install(monkeypatch, FakeMM({"ok": True, "data": REAL_POSITIONS_EMPTY}))
    assert broker.fetch_position("BTCUSDT") is None


def test_fetch_position_finds_matching_symbol(broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": [
        {"venue": "hyperliquid", "symbol": "ETH", "size": "2.0"},
        {"venue": "hyperliquid", "symbol": "BTC", "size": "-0.00599"},
    ]}))
    entry = broker.fetch_position("BTCUSDT")
    assert entry is not None and entry["symbol"] == "BTC"


def test_fetch_position_returns_none_when_size_is_zero(broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": [
        {"venue": "hyperliquid", "symbol": "BTC", "size": "0"},
    ]}))
    assert broker.fetch_position("BTCUSDT") is None


def test_size_decimals_comes_from_the_venue_not_a_hardcoded_guess(cold_broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": [REAL_MARKET_BTC]}))
    assert cold_broker.size_decimals_for("BTCUSDT") == 5


def test_market_meta_is_cached_so_it_stays_off_the_order_path(cold_broker, monkeypatch):
    """Each mm call costs tens of seconds; metadata must be fetched once."""
    fake = FakeMM({"ok": True, "data": [REAL_MARKET_BTC]})
    install(monkeypatch, fake)
    cold_broker.size_decimals_for("BTCUSDT")
    cold_broker.size_decimals_for("BTCUSDT")
    cold_broker.check_leverage_supported("BTCUSDT", 20)
    assert len(fake.calls) == 1


def test_size_decimals_falls_back_when_the_lookup_fails(cold_broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": False, "error": "network down"}))
    assert cold_broker.size_decimals_for("BTCUSDT") == cold_broker.settings.perps_size_decimals


def test_leverage_within_venue_maximum_is_accepted(cold_broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": [REAL_MARKET_BTC]}))
    # BTC on Hyperliquid reports maxLeverage 40, so 20x is fine.
    cold_broker.check_leverage_supported("BTCUSDT", 20)  # must not raise


# -- venue routing --------------------------------------------------------- #
def test_build_broker_routes_by_venue(monkeypatch):
    monkeypatch.setattr(
        "maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")

    hyperliquid = build_broker(Settings(mode=RunMode.TESTNET, venue=Venue.HYPERLIQUID))
    assert isinstance(hyperliquid, MetaMaskPerpsBroker)

    # sim/paper never touch the CLI regardless of venue
    assert isinstance(build_broker(Settings(mode=RunMode.SIM, venue=Venue.HYPERLIQUID)),
                      PaperBroker)


def test_live_mode_targets_mainnet(monkeypatch):
    monkeypatch.setattr(
        "maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")
    broker = MetaMaskPerpsBroker(Settings(mode=RunMode.LIVE, venue=Venue.HYPERLIQUID))
    assert broker.network == "mainnet"
