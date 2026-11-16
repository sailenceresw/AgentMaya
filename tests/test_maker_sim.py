"""Post-only entry simulation on the paper path. Invented prices throughout."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from maya.config import RunMode, Settings
from maya.data.models import Candle
from maya.execution.engine import ExecutionEngine
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes
from maya.research.costs import MAKER_FILLED, PRESETS, Sample, SampleList
from maya.research.harness import Hypothesis, evaluate
from maya.research.hypotheses import CATALOG
from maya.strategy.models import TradeIntent


class _Decision:
    approved, quantity, position_notional, leverage = True, 1.0, 100.0, 10


def _intent(direction="long"):
    if direction == "long":
        return TradeIntent("BTCUSDT", "long", "s", 100, 99.5, 102, 102, 8, expected_rr=4.0)
    return TradeIntent("BTCUSDT", "short", "s", 100, 100.5, 98, 98, 8, expected_rr=4.0)


def _bar(i, o, h, l, c):  # noqa: E741
    return Candle(timestamp=i * 60_000, open=o, high=h, low=l, close=c, volume=1)


def _engine(**kw):
    kw.setdefault("maker_fill_probability", 1.0)
    return ExecutionEngine(Settings(mode=RunMode.SIM, paper_entry_mode="post_only", **kw))


def test_immediate_mode_is_unchanged():
    engine = ExecutionEngine(Settings(mode=RunMode.SIM))
    assert engine.open(_intent(), _Decision(), ts=0) is True
    assert engine.has_position and engine.pending is None and engine.maker.placed == 0


def test_post_only_rests_instead_of_filling():
    engine = _engine()
    assert engine.open(_intent(), _Decision(), ts=0) is False
    assert not engine.has_position and engine.busy and engine.maker.placed == 1


def test_fills_when_price_trades_through_the_level():
    engine = _engine()
    engine.open(_intent(), _Decision(), ts=0)
    assert engine.manage(_bar(1, 100.1, 100.2, 99.9, 100.0)) is None
    assert engine.has_position and engine.pending is None
    assert engine.position.entry_price == 100 and engine.position.opened_ts == 60_000
    assert engine.pop_fill() is True and engine.pop_fill() is False
    assert engine.maker.filled == 1


def test_a_touch_is_not_a_fill():
    engine = _engine()
    engine.open(_intent(), _Decision(), ts=0)
    engine.manage(_bar(1, 100.2, 100.4, 100.0, 100.3))    # low == level, nobody sold below
    assert not engine.has_position


def test_unfilled_order_expires_and_counts_as_missed():
    engine = _engine(maker_order_ttl_candles=2)
    engine.open(_intent(), _Decision(), ts=0)
    engine.manage(_bar(1, 100.2, 100.5, 100.1, 100.4))
    assert engine.pending is not None                     # still waiting
    engine.manage(_bar(2, 100.4, 100.9, 100.3, 100.8))
    assert engine.pending is None and not engine.busy
    assert (engine.maker.missed, engine.maker.filled) == (1, 0)


def test_short_fills_when_price_trades_above_the_level():
    engine = _engine()
    engine.open(_intent("short"), _Decision(), ts=0)
    engine.manage(_bar(1, 99.9, 100.1, 99.8, 100.0))
    assert engine.has_position and engine.position.direction == "short"


def test_fill_probability_drops_some_orders_and_is_repeatable():
    def run():
        engine = _engine(maker_fill_probability=0.5)
        for i in range(200):
            engine.open(_intent(), _Decision(), ts=i)
            engine.manage(_bar(i, 100.1, 100.2, 99.9, 100.0))
            engine.position = None                        # flatten for the next order
        return engine.maker.filled, engine.maker.missed
    filled, missed = run()
    assert filled + missed == 200 and 70 < filled < 130
    assert run() == (filled, missed)                      # seeded


def test_stop_is_not_checked_on_the_fill_candle():
    engine = _engine()
    engine.open(_intent(), _Decision(), ts=0)
    assert engine.manage(_bar(1, 100.1, 100.2, 99.0, 99.2)) is None   # through level and stop
    assert engine.has_position
    result = engine.manage(_bar(2, 99.2, 99.3, 99.0, 99.1))
    assert result is not None and result.reason == "stop_loss"


def test_adverse_selection_compares_filled_with_missed():
    engine = _engine(maker_adverse_horizon_candles=2)
    # Order 1 fills (price dips through 100) and price keeps falling.
    engine.open(_intent(), _Decision(), ts=0)
    engine.manage(_bar(1, 100.0, 100.1, 99.8, 99.9))
    engine.position = None
    engine.manage(_bar(2, 99.9, 99.9, 99.6, 99.7))        # horizon: 99.7 vs 100 = -30 bps
    # Order 2 never fills (price runs away upward) and keeps rising.
    engine.open(_intent(), _Decision(), ts=3)
    engine.manage(_bar(3, 100.2, 100.6, 100.1, 100.5))
    engine.manage(_bar(4, 100.5, 100.9, 100.4, 100.8))    # horizon: 100.8 vs 100 = +80 bps
    s = engine.maker.summary()
    assert (s["placed"], s["filled"], s["missed"]) == (2, 1, 1)
    assert s["missed_share"] == 0.5
    assert s["filled_forward_bps"] == pytest.approx(-30.0)
    assert s["missed_forward_bps"] == pytest.approx(80.0)
    assert s["adverse_selection_bps"] == pytest.approx(110.0)


def test_real_order_modes_ignore_the_setting():
    s = Settings(mode=RunMode.SIM, paper_entry_mode="post_only")
    assert ExecutionEngine(s).post_only
    assert not s.uses_real_orders


def test_bad_settings_are_refused():
    with pytest.raises(ValidationError):
        Settings(paper_entry_mode="maker")
    with pytest.raises(ValidationError):
        Settings(maker_fill_probability=0)


def test_graph_counts_the_trade_at_the_fill_not_the_order(tmp_path, monkeypatch):
    nodes = TradingNodes(Settings(mode=RunMode.SIM, paper_entry_mode="post_only",
                                  maker_fill_probability=1.0),
                         TradeJournal(str(tmp_path / "j.db")), now_ts=0)
    state = {"candle": _bar(0, 100, 100.1, 99.9, 100), "intent": _intent(),
             "risk_decision": _Decision()}
    assert nodes.execution_node(state)["opened"] is False
    assert nodes.risk.account.trades_today == 0           # resting, not a trade yet
    assert nodes.execution.busy
    nodes.manage_node({"candle": _bar(1, 100.1, 100.2, 99.9, 100.0)})
    assert nodes.risk.account.trades_today == 1 and nodes.risk.account.open_positions == 1


def test_no_new_intent_while_an_order_rests(tmp_path, monkeypatch):
    from maya.signals.models import SignalObject
    nodes = TradingNodes(Settings(mode=RunMode.SIM, paper_entry_mode="post_only"),
                         TradeJournal(str(tmp_path / "j.db")), now_ts=0)
    nodes.execution.open(_intent(), _Decision(), ts=0)
    monkeypatch.setattr(nodes.strategy, "evaluate", lambda *a, **k: _intent())
    sig = SignalObject(timestamp=0, symbol="BTCUSDT", price=100, high=101, low=99)
    assert nodes.strategy_node({"candle": _bar(0, 100, 101, 99, 100), "signal": sig})["intent"] \
        is None


def test_maker_filled_preset_charges_maker_fees_without_a_second_haircut():
    assert PRESETS["maker_filled"] is MAKER_FILLED
    assert MAKER_FILLED.round_trip_bps == 4.0
    samples = [Sample(i, 0.001) for i in range(50)]
    assert MAKER_FILLED.fill(samples) == (samples, 0)


def test_generator_notes_reach_the_report():
    hyp = Hypothesis("h", "", lambda d: [], cost="maker_filled")
    samples = SampleList([Sample(i * 1000, 0.002) for i in range(200)],
                         notes=["BTCUSDT post-only: 10 placed, 6 filled, 4 missed"])
    rep = evaluate(hyp, samples, 0, 200_000, n_tests=1)
    assert rep.notes and "missed" in rep.format()


def test_maker_hypothesis_is_declared():
    hyp = CATALOG["maker_cvd_reversion"]
    assert hyp.cost == "maker_filled" and hyp.timeframe == "15m" and not hyp.blocked
