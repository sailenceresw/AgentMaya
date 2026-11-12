"""Tests for startup state reconciliation (spec §8.4)."""
from maya.config import RunMode, Settings
from maya.execution.brokers import PaperBroker
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes
from maya.risk.manager import SystemState
from maya.run import _reconcile


class _Notifier:
    enabled = False

    def send(self, *_):
        pass


def test_paper_broker_is_flat():
    b = PaperBroker(Settings(mode=RunMode.PAPER))
    assert b.open_state() == {"position_qty": 0.0, "open_orders": 0}


def test_reconcile_skipped_for_sim():
    settings = Settings(mode=RunMode.SIM, start_equity=1000)
    nodes = TradingNodes(settings, TradeJournal(db_path=":memory:"))
    assert _reconcile(settings, nodes, _Notifier()) is True


def test_reconcile_halts_on_existing_position(monkeypatch):
    class StubBroker:
        def __init__(self, *_):
            pass

        def open_state(self):
            return {"position_qty": 0.01, "open_orders": 2}

    monkeypatch.setattr("maya.execution.engine.build_broker", lambda s: StubBroker())
    settings = Settings(mode=RunMode.TESTNET, start_equity=1000)
    nodes = TradingNodes(settings, TradeJournal(db_path=":memory:"))
    ok = _reconcile(settings, nodes, _Notifier())
    assert ok is False
    assert nodes.risk.account.state is SystemState.HALTED


def test_reconcile_proceeds_when_flat(monkeypatch):
    class StubBroker:
        def __init__(self, *_):
            pass

        def open_state(self):
            return {"position_qty": 0.0, "open_orders": 0}

    monkeypatch.setattr("maya.execution.engine.build_broker", lambda s: StubBroker())
    settings = Settings(mode=RunMode.TESTNET, start_equity=1000)
    nodes = TradingNodes(settings, TradeJournal(db_path=":memory:"))
    assert _reconcile(settings, nodes, _Notifier()) is True
    assert nodes.risk.account.state is SystemState.RUNNING
