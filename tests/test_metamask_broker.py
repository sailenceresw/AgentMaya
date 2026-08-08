"""Tests for the MetaMask Agent Wallet / Hyperliquid broker.

No network and no `mm` process: :func:`subprocess.run` is replaced with a
recorder that returns crafted ``{"ok": ...}`` envelopes. That envelope shape
is the part actually confirmed against the installed CLI (v6.0.0), so what
these tests pin down is *our* side of the contract — the argv we construct,
the safety behaviour on failure, and the tolerance of the payload parsing.
They cannot prove the venue accepts these orders; only a testnet run does
that (see docs/RUNNING.md).
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


@pytest.fixture
def broker(settings, monkeypatch) -> MetaMaskPerpsBroker:
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
    broker = MetaMaskPerpsBroker(settings)
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


def test_fetch_position_finds_matching_symbol(broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": {"positions": [
        {"coin": "ETH", "szi": 2.0},
        {"coin": "BTC", "szi": -0.034},
    ]}}))
    entry = broker.fetch_position("BTCUSDT")
    assert entry is not None and entry["coin"] == "BTC"


def test_fetch_position_returns_none_when_flat(broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": {"positions": [
        {"coin": "BTC", "szi": 0.0},
    ]}}))
    assert broker.fetch_position("BTCUSDT") is None


def test_fetch_position_tolerates_bare_list_payload(broker, monkeypatch):
    """The inner payload shape is unverified, so a top-level list must parse
    as readily as a {"positions": [...]} wrapper."""
    install(monkeypatch, FakeMM({"ok": True, "data": [{"symbol": "BTC", "size": -0.034}]}))
    assert broker.fetch_position("BTCUSDT") is not None


def test_fetch_collateral_reads_several_key_spellings(broker, monkeypatch):
    install(monkeypatch, FakeMM({"ok": True, "data": {"accountValue": "1234.56"}}))
    assert broker.fetch_collateral_usd() == pytest.approx(1234.56)


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
