"""Tests for the Layer 7 monitoring API and the journal's thread safety.

The thread-safety tests are not incidental: the API is the third thread to
touch the journal, and adding it exposed that the *second* one
(PositionMonitor) had been silently failing every write.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from maya.api.server import MonitorServer, build_snapshot
from maya.config import RunMode, Settings
from maya.execution.engine import TradeResult
from maya.execution.models import Position
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes

TOKEN = "test-token-abc"


def _result(pnl: float = 18.16, result: str = "win") -> TradeResult:
    pos = Position(
        symbol="BTCUSDT", direction="short", setup_name="liquidity_sweep_choch",
        entry_price=66800.0, stop_loss=67388.0, take_profit=63700.0,
        quantity=0.00599, notional=400.0, leverage=20, opened_ts=1700000600000,
        confluence_score=7, rr_planned=5.2,
    )
    return TradeResult(pos, 63700.0, pnl, 0.4, 5.2, result, 1700000900000, "take_profit")


# --------------------------------------------------------------------------- #
# Journal thread safety
# --------------------------------------------------------------------------- #
def test_journal_accepts_writes_from_another_thread(tmp_path):
    """Regression: sqlite3 connections are thread-bound by default, so
    PositionMonitor's poll thread raised ProgrammingError on every
    exchange-fill close. The monitor's catch-all swallowed it, meaning the
    trade was never journaled AND risk.on_trade_closed never ran — so
    consecutive_losses, the daily-loss halt and the kill switch silently
    stopped updating on testnet/live."""
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    error: dict[str, str] = {}

    def worker() -> None:
        try:
            journal.record(_result(), [])
        except Exception as exc:  # noqa: BLE001
            error["exc"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert not error, error
    assert journal.metrics()["trades"] == 1
    journal.close()


def test_journal_reads_are_safe_from_many_threads(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_result(), [])
    errors: list[str] = []

    def reader() -> None:
        try:
            for _ in range(20):
                journal.metrics()
                journal.recent(5)
                journal.session_state()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    journal.close()


def test_recent_returns_newest_first_and_clamps_limit(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_result(pnl=1.0), [])
    journal.record(_result(pnl=2.0), [])
    rows = journal.recent(500)          # must clamp, not explode
    assert len(rows) == 2
    assert {"setup_name", "direction", "pnl_usdt", "result"} <= set(rows[0])
    journal.close()


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_api(tmp_path):
    """A real server on an ephemeral port, fed by a real scenario run."""
    settings = Settings(mode=RunMode.SIM, scenario=True, max_candles=0,
                        api_enabled=True, api_token=TOKEN,
                        api_host="127.0.0.1", api_port=0)
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    nodes = TradingNodes(settings, journal)
    graph = build_graph(nodes)

    from maya.data.ingestion import build_feed
    processed = 0
    for candle in build_feed(settings).candles():
        graph.invoke({"candle": candle})
        processed += 1

    server = MonitorServer(
        settings,
        snapshot_fn=lambda: build_snapshot(settings, nodes, journal, processed),
        trades_fn=journal.recent,
        halt_fn=lambda reason: nodes.risk.manual_halt(),
    )
    server.start()
    yield server, nodes
    server.stop()
    journal.close()


def _call(server, path, token=TOKEN, method="GET", body=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_server_refuses_to_start_without_a_token():
    """It controls a trading system; defaulting to unauthenticated is not an
    acceptable failure mode."""
    with pytest.raises(ValueError, match="MAYA_API_TOKEN"):
        MonitorServer(Settings(api_enabled=True), lambda: {}, lambda n: [],
                      lambda r: None)


def test_health_is_reachable_without_a_token(live_api):
    server, _ = live_api
    status, body = _call(server, "/api/health", token=None)
    assert status == 200
    # Lets a client distinguish "wrong address" from "wrong token"...
    assert json.loads(body)["ok"] is True
    # ...without leaking any account state.
    assert "equity" not in body


def test_everything_else_requires_the_token(live_api):
    server, _ = live_api
    for path in ("/", "/api/status", "/api/trades"):
        assert _call(server, path, token="wrong")[0] == 401
        assert _call(server, path, token=None)[0] == 401


def test_status_reports_real_account_state(live_api):
    server, nodes = live_api
    status, body = _call(server, "/api/status")
    assert status == 200
    data = json.loads(body)["data"]
    assert data["account"]["equity"] == pytest.approx(nodes.risk.account.equity, abs=0.01)
    assert data["run"]["trades"] == 1
    assert data["config"]["leverage"] == 20
    assert data["config"]["sizing_mode"] == "margin"


def test_trades_endpoint_returns_the_journal(live_api):
    server, _ = live_api
    status, body = _call(server, "/api/trades?limit=5")
    trades = json.loads(body)["data"]
    assert status == 200
    assert trades and trades[0]["setup_name"] == "liquidity_sweep_choch"


def test_dashboard_is_served_as_html(live_api):
    server, _ = live_api
    status, body = _call(server, "/")
    assert status == 200
    assert body.startswith("<!doctype html>")
    assert "HALT TRADING" in body


def test_halt_moves_the_system_to_a_safer_state(live_api):
    server, nodes = live_api
    assert nodes.risk.account.state.value == "running"
    status, body = _call(server, "/api/halt", method="POST", body={"reason": "test"})
    assert status == 200
    assert json.loads(body)["data"]["account"]["state"] == "halted"
    assert nodes.risk.account.state.value == "halted"


def test_halt_requires_the_token(live_api):
    server, nodes = live_api
    assert _call(server, "/api/halt", token="wrong", method="POST")[0] == 401
    assert nodes.risk.account.state.value == "running"


def test_there_is_no_endpoint_that_resumes_or_opens_a_trade(live_api):
    """The API must only ever move the system toward safety. A stolen token
    should be able to stop the agent, never to make it trade."""
    server, _ = live_api
    for path in ("/api/resume", "/api/start", "/api/open", "/api/trade",
                 "/api/config"):
        assert _call(server, path, method="POST")[0] == 404
        assert _call(server, path)[0] == 404


def test_unknown_routes_404(live_api):
    server, _ = live_api
    assert _call(server, "/api/nope")[0] == 404
