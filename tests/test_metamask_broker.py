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
    # No real waiting: a resting entry is checked once, then cancelled.
    return Settings(mode=RunMode.TESTNET, venue=Venue.HYPERLIQUID, symbol="BTCUSDT",
                    perps_limit_fill_timeout_s=0, perps_fill_poll_s=0)


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


# `mm perps open` results in the v7 shape. Unlike the REAL_* fixtures below
# these were NOT captured from a live session: they are built from the
# result mapping in the CLI's own v7.1.0 source (filled -> orderId, status,
# averagePrice, filledSize; resting -> orderId, status; error -> status
# "rejected", error). Statuses are "filled" | "resting" | "submitted" |
# "rejected".
V7_OPEN_FILLED = {"venue": "hyperliquid", "symbol": "BTC", "orderId": "1",
                  "status": "filled", "averagePrice": "66795.5", "filledSize": "0.034"}
V7_OPEN_RESTING = {"venue": "hyperliquid", "symbol": "BTC", "orderId": "77",
                   "status": "resting"}
V7_OPEN_REJECTED = {"venue": "hyperliquid", "symbol": "BTC", "status": "rejected",
                    "error": "Order could not immediately match against any resting orders."}
# v7 position rows: absolute size plus side, entryPrice, liquidationPrice.
V7_POSITION_SHORT = {"venue": "hyperliquid", "symbol": "BTC", "side": "short",
                     "size": "0.034", "entryPrice": "66800.0", "positionValue": "2271.2",
                     "unrealizedPnl": "0", "marginUsed": "454.24", "leverage": 5,
                     "liquidationPrice": "79500.0"}


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
    fake = FakeMM({"ok": True, "data": V7_OPEN_FILLED}, {"ok": True, "data": {}})
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
        {"ok": True, "data": V7_OPEN_FILLED},          # open succeeds
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
        {"ok": True, "data": V7_OPEN_FILLED},
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


# --------------------------------------------------------------------------- #
# CLI v7 behaviour: order status, resting entries, output forms, telemetry
# --------------------------------------------------------------------------- #
def test_filled_entry_records_the_venue_fill_not_the_plan(broker, intent, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": V7_OPEN_FILLED}, {"ok": True, "data": {}}))
    assert broker.open_position(intent, 0.034, 5) is True
    assert broker.last_fill.price == pytest.approx(66795.5)
    assert broker.last_fill.size == pytest.approx(0.034)


def test_venue_rejected_entry_never_attaches_sl_tp(broker, intent, monkeypatch):
    """`ok: true` with status "rejected" is a failed order, not a success."""
    fake = FakeMM({"ok": True, "data": V7_OPEN_REJECTED})
    install(monkeypatch, fake)
    assert broker.open_position(intent, 0.034, 5) is False
    assert len(fake.calls) == 1
    assert broker.last_fill is None


def test_unfilled_resting_entry_is_cancelled_not_left_naked(broker, intent, monkeypatch):
    """The bug this guards: a resting limit has no position yet, so the SL/TP
    `modify` failed, the unwind `close` found nothing, and the limit order was
    left on the book with no stop — to fill later as a naked position."""
    fake = FakeMM(
        {"ok": True, "data": V7_OPEN_RESTING},
        {"ok": True, "data": []},          # not filled within the wait
        {"ok": True, "data": {}},          # cancel
        {"ok": True, "data": []},          # still flat after the cancel
    )
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is False
    cancel = fake.command(2)
    assert "perps cancel" in cancel and "--order-id 77" in cancel and "--symbol BTC" in cancel
    assert not any("perps modify" in fake.command(i) for i in range(len(fake.calls)))


def test_resting_entry_that_fills_gets_protected(broker, intent, monkeypatch):
    fake = FakeMM(
        {"ok": True, "data": V7_OPEN_RESTING},
        {"ok": True, "data": [V7_POSITION_SHORT]},   # filled while waiting
        {"ok": True, "data": {}},                    # SL/TP attach
    )
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is True
    assert "perps modify" in fake.command(2) and "--sl 67388" in fake.command(2)
    assert broker.last_fill.price == pytest.approx(66800.0)


def test_partial_fill_is_cancelled_then_protected_at_its_real_size(broker, intent, monkeypatch):
    partial = dict(V7_POSITION_SHORT, size="0.01")
    fake = FakeMM(
        {"ok": True, "data": V7_OPEN_RESTING},
        {"ok": True, "data": [partial]},     # part-filled at the deadline
        {"ok": True, "data": {}},            # cancel the remainder
        {"ok": True, "data": [partial]},     # what is actually open
        {"ok": True, "data": {}},            # SL/TP attach
    )
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is True
    assert "perps cancel" in fake.command(2)
    assert "perps modify" in fake.command(4)
    assert broker.last_fill.size == pytest.approx(0.01)


def test_failed_cancel_of_resting_entry_reports_failure(broker, intent, monkeypatch):
    fake = FakeMM(
        {"ok": True, "data": V7_OPEN_RESTING},
        {"ok": True, "data": []},
        {"ok": False, "error": {"code": "ORDER_NOT_FOUND", "message": "no such order"}},
        {"ok": True, "data": []},
    )
    install(monkeypatch, fake)
    assert broker.open_position(intent, 0.034, 5) is False


def test_resting_entry_without_order_id_cancels_only_its_own_order(broker, intent, monkeypatch):
    submitted = {"venue": "hyperliquid", "symbol": "BTC", "status": "submitted"}
    fake = FakeMM(
        {"ok": True, "data": submitted},
        {"ok": True, "data": []},
        {"ok": True, "data": [   # `perps orders`
            {"orderId": "5", "symbol": "ETH", "side": "short", "limitPrice": "66800"},
            {"orderId": "6", "symbol": "BTC", "side": "short", "limitPrice": "70000"},
            {"orderId": "7", "symbol": "BTC", "side": "short", "limitPrice": "66800"},
        ]},
        {"ok": True, "data": {}},
        {"ok": True, "data": []},
    )
    install(monkeypatch, fake)

    assert broker.open_position(intent, 0.034, 5) is False
    cancels = [fake.command(i) for i in range(len(fake.calls)) if "perps cancel" in fake.command(i)]
    assert len(cancels) == 1 and "--order-id 7" in cancels[0]


def test_signing_commands_carry_the_wallet_timeout(broker, intent, position, monkeypatch):
    fake = FakeMM({"ok": True, "data": V7_OPEN_FILLED}, {"ok": True, "data": {}},
                  {"ok": True, "data": {}}, {"ok": True, "data": []})
    install(monkeypatch, fake)
    broker.open_position(intent, 0.034, 5)
    broker.close_position(position, 63700.0, "take_profit")
    broker.perps("positions")

    for i in range(3):   # open, modify, close
        assert "--wallet-timeout 90" in fake.command(i)
    assert "--wallet-timeout" not in fake.command(3)   # reads never sign


def test_error_envelope_on_stderr_keeps_code_and_hint(broker, monkeypatch):
    """Captured from CLI v7.1.0: failures print the envelope to STDERR and
    leave stdout empty."""
    stderr = json.dumps({"ok": False, "error": {
        "code": "AUTH_FAILED",
        "message": "No CLI refresh token available — run `mm login` to sign in.",
        "hint": "Run `mm login` to sign in."}}, indent=2)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", stderr))
    with pytest.raises(MMError, match="AUTH_FAILED") as err:
        broker.perps("balance")
    assert err.value.code == "AUTH_FAILED"


def test_stream_output_with_mfa_notice_returns_the_summary(broker, monkeypatch):
    """Once a command emits a notice (e.g. a Guard Mode approval wait) the CLI
    switches to one JSON object per line. Parsing stdout as one document
    used to turn a successful order into a 'non-JSON output' error."""
    stdout = "\n".join([
        json.dumps({"_notice": {"kind": "AWAITING_MFA", "pollingId": "p-1",
                                "message": "Approve in MetaMask"}}),
        json.dumps({"_summary": V7_OPEN_FILLED}),
    ])
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout, ""))
    assert broker.perps("open")["status"] == "filled"


def test_stream_error_on_stderr_is_a_failure(broker, monkeypatch):
    stdout = json.dumps({"_notice": {"kind": "AWAITING_MFA", "pollingId": "p-1"}})
    stderr = json.dumps({"_error": {"code": "MFA_TIMEOUT", "message": "approval expired"}})
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout, stderr))
    with pytest.raises(MMError, match="MFA_TIMEOUT"):
        broker.perps("open")


def test_timeout_after_printing_a_result_uses_the_result(broker, monkeypatch):
    """Observed with v7.1.0: the CLI prints its result, then hangs flushing
    analytics until killed. The printed result is conclusive."""
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd, 120, output=json.dumps({"ok": True, "data": [V7_POSITION_SHORT]}), stderr="")

    monkeypatch.setattr(subprocess, "run", hang)
    assert broker.fetch_position("BTCUSDT")["symbol"] == "BTC"


def test_timeout_with_partial_bytes_output_is_still_unknown(broker, monkeypatch):
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 120, output=b'{"_notice": {"kind": "AWAITING_MFA"}}')

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(MMError, match="UNKNOWN"):
        broker.perps("open")


def test_telemetry_is_disabled_for_the_subprocess_by_default(broker, monkeypatch):
    seen = {}

    def record(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"ok": True, "data": []}), "")

    monkeypatch.setattr(subprocess, "run", record)
    broker.perps("positions")
    assert seen.get("MM_TELEMETRY_DISABLED") == "1"


def test_telemetry_can_be_opted_into(settings, monkeypatch):
    monkeypatch.setattr("maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")
    monkeypatch.delenv("MM_TELEMETRY_DISABLED", raising=False)
    broker = MetaMaskPerpsBroker(settings.model_copy(update={"mm_telemetry": True}))
    assert "MM_TELEMETRY_DISABLED" not in broker._env


def test_process_timeout_must_outlast_the_wallet_timeout():
    with pytest.raises(ValueError, match="must exceed"):
        Settings(mm_timeout_s=60, mm_wallet_timeout_s=90)
    with pytest.raises(ValueError, match="1..600"):
        Settings(mm_timeout_s=900, mm_wallet_timeout_s=700)


# -- preflight (maya.check_metamask) ---------------------------------------- #
# `mm doctor` captured verbatim from CLI v7.1.0 (signed out), trimmed.
REAL_DOCTOR_SIGNED_OUT = {
    "cli": "7.1.0", "env": "prod", "authenticated": False, "initialized": False,
    "hints": ["Not authenticated for env prod. Run `mm login`."],
}


def _diagnose(settings, monkeypatch, *responses):
    from maya.check_metamask import run_diagnostics

    monkeypatch.setattr("maya.execution.metamask_broker.resolve_mm_binary", lambda: "mm.cmd")
    fake = FakeMM(*responses)
    install(monkeypatch, fake)
    return run_diagnostics(settings), fake


def test_preflight_fails_fast_when_signed_out(settings, monkeypatch):
    report, fake = _diagnose(settings, monkeypatch,
                             {"ok": True, "data": REAL_DOCTOR_SIGNED_OUT})
    assert not report.ok
    assert any("mm login" in e for e in report.errors)
    assert len(fake.calls) == 1   # nothing else attempted


def test_preflight_requires_an_initialised_wallet(settings, monkeypatch):
    doctor = dict(REAL_DOCTOR_SIGNED_OUT, authenticated=True, initialized=False)
    report, _ = _diagnose(settings, monkeypatch, {"ok": True, "data": doctor})
    assert not report.ok
    assert any("mm init" in e for e in report.errors)


def test_preflight_healthy_path_warns_on_old_cli_and_beast_mode(monkeypatch):
    live = Settings(mode=RunMode.LIVE, venue=Venue.HYPERLIQUID, symbol="BTCUSDT")
    doctor = dict(REAL_DOCTOR_SIGNED_OUT, cli="6.0.0", authenticated=True, initialized=True)
    funded = dict(REAL_BALANCE, totalBalance="500.0")
    report, _ = _diagnose(
        live, monkeypatch,
        {"ok": True, "data": doctor},
        {"ok": True, "data": {"mode": "beast", "address": "0xabc"}},
        {"ok": True, "data": funded},
        {"ok": True, "data": [REAL_MARKET_BTC]},
        {"ok": True, "data": REAL_POSITIONS_EMPTY},
        {"ok": True, "data": REAL_ORDERS_EMPTY},
    )
    assert report.ok
    assert report.usdt_balance == pytest.approx(500.0)
    assert any("older than v7" in w for w in report.warnings)
    assert any("Beast Mode" in w for w in report.warnings)


def test_preflight_falls_back_to_auth_status_on_cli_without_doctor(settings, monkeypatch):
    report, fake = _diagnose(
        settings, monkeypatch,
        {"ok": False, "error": {"code": "UNKNOWN_COMMAND", "message": "doctor"}},
        {"ok": True, "data": {"authenticated": False}},
    )
    assert not report.ok
    assert "auth status" in fake.command(1)
