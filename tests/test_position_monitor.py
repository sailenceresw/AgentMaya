"""Tests for the exchange-fill reconciliation path used by the position
monitor (testnet/live) — see maya/execution/monitor.py and
ExecutionEngine.reconcile_external_close."""
import pytest

from maya.config import RunMode, Settings
from maya.execution.engine import ExecutionEngine
from maya.execution.monitor import PositionMonitor
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager
from maya.strategy.models import TradeIntent


def _settings():
    return Settings(mode=RunMode.SIM, start_equity=1000)  # PaperBroker — no network


def _intent():
    return TradeIntent("BTCUSDT", "long", "value_area_absorption",
                       entry_price=100, stop_loss=95, take_profit_1=120,
                       take_profit_2=125, confluence_score=8, expected_rr=5.0)


class _Decision:
    def __init__(self):
        self.approved = True
        self.quantity = 1.0
        self.position_notional = 100.0
        self.leverage = 5


def test_reconcile_external_close_does_not_call_broker_close(monkeypatch):
    """The exchange already filled the order — reconcile must NOT send
    another close order (that was the original bug)."""
    engine = ExecutionEngine(_settings())
    engine.open(_intent(), _Decision(), ts=0)

    called = {"n": 0}
    monkeypatch.setattr(engine.broker, "close_position",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    result = engine.reconcile_external_close(exit_price=120, ts=1000, reason="exchange_fill")
    assert result is not None
    assert result.result == "win"
    assert called["n"] == 0            # broker.close_position was never invoked
    assert engine.position is None     # local state is now flat


def test_reconcile_external_close_uses_actual_fees_when_given():
    engine = ExecutionEngine(_settings())
    engine.open(_intent(), _Decision(), ts=0)
    result = engine.reconcile_external_close(exit_price=120, ts=1000,
                                             reason="exchange_fill", fees_usdt=1.23)
    assert result.fees_usdt == 1.23


def test_reconcile_external_close_noop_when_no_position():
    engine = ExecutionEngine(_settings())
    assert engine.reconcile_external_close(exit_price=100, ts=0, reason="x") is None


def test_manage_is_a_noop_after_monitor_already_closed(monkeypatch):
    """If the monitor beats the candle to it, the next candle's manage()
    call must be a harmless no-op, not a duplicate close attempt."""
    from maya.data.models import Candle

    engine = ExecutionEngine(_settings())
    engine.open(_intent(), _Decision(), ts=0)
    engine.reconcile_external_close(exit_price=120, ts=500, reason="exchange_fill")

    candle = Candle(timestamp=60_000, open=120, high=125, low=119, close=121, volume=10)
    assert engine.manage(candle) is None  # no crash, no phantom second close


class _FakeExchange:
    """Minimal stand-in for ccxt.binanceusdm used by PositionMonitor."""

    def __init__(self, contracts_remaining: float, fill_price: float):
        self._contracts_remaining = contracts_remaining
        self._fill_price = fill_price

    def fetch_positions(self, symbols):
        return [{"symbol": symbols[0], "contracts": self._contracts_remaining}]

    def fetch_my_trades(self, symbol, limit=5):
        return [{
            "price": self._fill_price,
            "info": {"reduceOnly": True},
            "fee": {"cost": 0.42},
        }]


def test_position_monitor_detects_flat_exchange_and_closes_locally():
    settings = _settings()
    engine = ExecutionEngine(settings)
    engine.open(_intent(), _Decision(), ts=0)

    journal = TradeJournal(db_path=":memory:")
    risk = RiskManager(settings, 1000)
    monitor = PositionMonitor(settings, engine, journal, risk, poll_interval_s=0.01)
    monitor._exchange = _FakeExchange(contracts_remaining=0.0, fill_price=120.0)

    monitor._poll_once()

    assert engine.position is None
    assert journal.metrics()["trades"] == 1
    assert journal.metrics()["win_rate"] == 100.0


def test_position_monitor_leaves_position_open_while_exchange_still_shows_it():
    settings = _settings()
    engine = ExecutionEngine(settings)
    engine.open(_intent(), _Decision(), ts=0)

    journal = TradeJournal(db_path=":memory:")
    risk = RiskManager(settings, 1000)
    monitor = PositionMonitor(settings, engine, journal, risk, poll_interval_s=0.01)
    monitor._exchange = _FakeExchange(contracts_remaining=1.0, fill_price=120.0)

    monitor._poll_once()

    assert engine.position is not None  # still open — exchange agrees
    assert journal.metrics().get("trades", 0) == 0


# -- exit estimation when the venue gives no fill (Hyperliquid) -------------- #
def _hl_monitor(monkeypatch, mark):
    from maya.config import Venue

    settings = Settings(mode=RunMode.SIM, venue=Venue.HYPERLIQUID, start_equity=1000)
    engine = ExecutionEngine(settings)
    engine.open(_intent(), _Decision(), ts=0)
    monitor = PositionMonitor(settings, engine, TradeJournal(db_path=":memory:"),
                              RiskManager(settings, 1000))
    monkeypatch.setattr(type(settings), "uses_metamask", property(lambda self: True))
    monkeypatch.setattr(monitor, "_mark_price", lambda symbol: mark)
    return monitor, engine


def test_stop_out_is_journaled_as_a_loss_not_a_win(monkeypatch):
    """Regression: the estimate returned the take-profit unconditionally, so a
    stop-out on Hyperliquid was journaled as a winning trade."""
    monitor, engine = _hl_monitor(monkeypatch, mark=95.3)   # stop 95, target 120
    price, fees, reason = monitor._last_fill(engine.position)
    assert (price, reason) == (95, "stop_loss")
    assert fees is None   # the engine charges its fee model


def test_target_fill_is_journaled_as_a_win(monkeypatch):
    monitor, engine = _hl_monitor(monkeypatch, mark=119.0)
    assert monitor._last_fill(engine.position)[2] == "take_profit"


def test_no_mark_price_assumes_the_stop(monkeypatch):
    monitor, engine = _hl_monitor(monkeypatch, mark=None)
    assert monitor._last_fill(engine.position)[:3:2] == (95, "stop_loss")


def test_r_multiple_survives_a_breakeven_stop_move():
    """Regression: R was measured against the *current* stop. After a
    breakeven move that is the entry price, risk read as 0 and every such
    trade — winners included — was journaled at 0R."""
    engine = ExecutionEngine(_settings())
    engine.open(_intent(), _Decision(), ts=0)       # long 100, stop 95, target 120
    engine.position.stop_loss = 100.0               # moved to breakeven
    engine.position.moved_to_breakeven = True
    result = engine.reconcile_external_close(exit_price=120, ts=1, reason="take_profit")
    assert result.rr_realized == pytest.approx(20 / 5)   # +20 on an initial 5 risk


def test_r_multiple_after_breakeven_exit_is_zero_not_undefined():
    engine = ExecutionEngine(_settings())
    engine.open(_intent(), _Decision(), ts=0)
    engine.position.stop_loss = 100.0
    result = engine.reconcile_external_close(exit_price=100, ts=1, reason="stop_loss")
    assert result.rr_realized == 0.0
