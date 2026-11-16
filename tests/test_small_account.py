"""The small-account rules: limits, sizing, profit protection, lessons veto.

Prices and trades here are invented fixtures chosen to make the arithmetic
easy to check by hand.
"""
from __future__ import annotations

import pytest
from loguru import logger
from pydantic import ValidationError

from maya.config import RiskLimits, RunMode, Settings
from maya.data.models import Candle
from maya.execution.engine import ExecutionEngine, TradeResult
from maya.execution.models import Position
from maya.journal.store import TradeJournal
from maya.learning.lessons import LessonsReviewer, derive_lesson, jaccard
from maya.nodes import TradingNodes
from maya.risk.manager import RiskManager, SystemState
from maya.risk.sizing import (
    calculate_margin_notional,
    calculate_pct_notional,
    calculate_position_notional,
    max_risk_usd,
)
from maya.strategy.models import TradeIntent

_DAY = 86_400_000


def _ten_dollar(**kw) -> Settings:
    return Settings(mode=RunMode.SIM, start_equity=10, leverage=10,
                    sizing_mode="risk_pct", **kw)


def _intent(direction="long", entry=100.0, sl=99.5, tp=103.0, confluences=None):
    return TradeIntent("BTCUSDT", direction, "liquidity_sweep_choch", entry_price=entry,
                       stop_loss=sl, take_profit_1=tp, take_profit_2=tp,
                       confluence_score=8, expected_rr=abs(tp - entry) / abs(entry - sl),
                       confluences=confluences or ["setup_match", "htf_alignment", "cvd"])


# -- hard limits ------------------------------------------------------------ #
def test_the_limits_are_the_ones_agreed():
    assert RiskLimits.MAX_RISK_PER_TRADE_PCT == 3.0
    assert RiskLimits.MAX_TRADES_PER_DAY == 10
    assert RiskLimits.DAILY_LOSS_LIMIT_PCT == 10.0
    assert RiskLimits.MAX_LEVERAGE == 10
    assert RiskLimits.POSITION_NOTIONAL_CAP_MULT == 10.0
    assert RiskLimits.MIN_RR_FOR_ENTRY == 4.0


def test_leverage_above_ten_is_refused():
    with pytest.raises(ValidationError, match="MAX_LEVERAGE"):
        Settings(mode=RunMode.SIM, leverage=11)


def test_risk_pct_above_three_is_refused():
    with pytest.raises(ValidationError, match="MAX_RISK_PER_TRADE_PCT"):
        Settings(mode=RunMode.SIM, risk_per_trade_pct=3.5)


def test_risk_and_lessons_code_import_no_llm():
    """Checked on the import statements themselves, so a comment that mentions
    the LLM veto does not count but a real import would."""
    import ast

    import maya.config as config
    import maya.learning.lessons as lessons
    import maya.risk.manager as manager
    import maya.risk.sizing as sizing
    for mod in (config, manager, sizing, lessons):
        tree = ast.parse(open(mod.__file__).read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        bad = [n for n in names if "anthropic" in n or "llm" in n.lower() or "langchain" in n]
        assert not bad, f"{mod.__name__} imports {bad}"


# -- sizing on $10 ---------------------------------------------------------- #
def test_ten_dollar_account_risks_thirty_cents():
    assert max_risk_usd(10) == pytest.approx(0.30)
    # 0.5% stop: $0.30 / 0.005 = $60 notional, under the $100 cap.
    assert calculate_pct_notional(10, 3.0, entry=100, sl=99.5) == pytest.approx(60.0)


def test_tight_stop_is_clamped_by_the_ten_times_cap():
    # 0.2% stop would want $150; the cap is 10 x $10.
    assert calculate_pct_notional(10, 3.0, entry=100, sl=99.8) == pytest.approx(100.0)


def test_pct_ceiling_binds_in_every_sizing_mode():
    # Asking for $20 of risk on a $10 account still risks only $0.30.
    assert calculate_position_notional(10, 20.0, entry=100, sl=99.5) == pytest.approx(60.0)
    assert calculate_margin_notional(10, 20.0, 10, entry=100, sl=99.5) == pytest.approx(60.0)


def test_large_account_is_still_bound_by_the_usd_ceiling():
    assert max_risk_usd(1000) == RiskLimits.MAX_RISK_PER_TRADE_USD


def test_ten_dollar_account_can_trade_and_is_sized_by_pct():
    rm = RiskManager(_ten_dollar(), 10)
    rm.check_kill_switch(0)
    assert rm.account.state is SystemState.RUNNING     # the old $50 floor halted here
    decision = rm.screen(_intent(), now_ts=0)
    assert decision.approved, decision.reason
    assert decision.position_notional == pytest.approx(60.0)
    assert decision.leverage == 10


def test_small_account_floor_is_half_the_starting_equity():
    assert RiskLimits.equity_floor(10) == 5.0
    assert RiskLimits.equity_floor(1000) == 50.0        # unchanged for larger accounts
    rm = RiskManager(_ten_dollar(), 10)
    rm.account.equity = 4.99
    rm.check_kill_switch(0)
    assert rm.account.state is SystemState.HALTED


def test_eleventh_trade_of_the_day_is_refused():
    rm = RiskManager(_ten_dollar(), 10)
    for _ in range(10):
        assert rm.screen(_intent(), now_ts=0).approved
        rm.on_trade_opened()
        rm.account.open_positions = 0
    decision = rm.screen(_intent(), now_ts=0)
    assert not decision.approved and "max trades per day" in decision.reason


def test_fee_drag_is_warned_about_but_does_not_block():
    seen = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        # $100 notional at 10 bps round trip is $0.10, exactly 1% of $10;
        # Hyperliquid through MetaMask costs more, so the warning fires.
        rm = RiskManager(_ten_dollar(venue="hyperliquid"), 10)
        decision = rm.screen(_intent(sl=99.8, tp=105.0), now_ts=0)
    finally:
        logger.remove(sink)
    assert decision.approved, decision.reason
    assert any("fee drag" in m for m in seen)


def test_target_under_five_round_trips_is_refused():
    # 0.3% target against a 0.1% round trip: fees would take a third of it.
    rm = RiskManager(_ten_dollar(min_roi_pct=0.1), 10)
    decision = rm.screen(_intent(sl=99.94, tp=100.3), now_ts=0)
    assert not decision.approved and "fees" in decision.reason


# -- profit protection ------------------------------------------------------ #
class _Decision:
    approved, quantity, position_notional, leverage = True, 1.0, 100.0, 10


def _bar(ts, o, h, l, c):  # noqa: E741
    return Candle(timestamp=ts * 60_000, open=o, high=h, low=l, close=c, volume=1)


def _open(direction="long", **kw):
    engine = ExecutionEngine(Settings(mode=RunMode.SIM, start_equity=1000, **kw))
    if direction == "long":
        engine.open(_intent("long", 100, 99.5, 104), _Decision(), ts=0)
    else:
        engine.open(_intent("short", 100, 100.5, 96), _Decision(), ts=0)
    return engine


def test_stop_stays_put_below_the_trigger():
    engine = _open()
    engine.manage(_bar(1, 100, 100.5, 99.9, 100.5))      # +0.5%, trigger is 0.6%
    assert engine.position.stop_loss == 99.5
    assert not engine.position.moved_to_breakeven


def test_lock_moves_stop_to_breakeven_plus_fees_then_trails():
    engine = _open()
    engine.manage(_bar(1, 100, 100.7, 100.0, 100.7))     # +0.7% close: lock fires
    pos = engine.position
    fees = 100 * engine.fees.worst_round_trip(engine.order_type)
    assert pos.moved_to_breakeven
    # Trail from the 100.7 high is 100.398, already above breakeven plus fees.
    assert pos.stop_loss == pytest.approx(100.7 * 0.997)
    assert pos.stop_loss > 100 + fees

    engine.manage(_bar(2, 100.7, 101.5, 100.6, 101.4))   # new best: 101.5
    assert pos.stop_loss == pytest.approx(101.5 * 0.997)


def test_lock_alone_covers_the_round_trip():
    # A wide trail leaves the breakeven-plus-fees lock as the active stop.
    engine = _open(profit_trail_pct=5.0)
    engine.manage(_bar(1, 100, 100.7, 100.0, 100.7))
    fees = 100 * engine.fees.worst_round_trip(engine.order_type)
    assert engine.position.stop_loss == pytest.approx(100 + fees)
    result = engine.manage(_bar(2, 100.7, 100.7, 99.0, 99.2))   # reverses through it
    assert result.reason == "stop_loss" and result.pnl_usdt == pytest.approx(0.0, abs=1e-9)


def test_trailing_stop_never_moves_back():
    engine = _open()
    engine.manage(_bar(1, 100, 100.7, 100.0, 100.7))
    engine.manage(_bar(2, 100.7, 101.5, 100.9, 101.4))
    high_water = engine.position.stop_loss
    engine.manage(_bar(3, 101.4, 101.45, 101.25, 101.3))  # lower high, above the stop
    assert engine.position.stop_loss == high_water


def test_trailed_stop_out_books_a_win():
    engine = _open()
    engine.manage(_bar(1, 100, 100.7, 100.0, 100.7))
    engine.manage(_bar(2, 100.7, 101.5, 100.9, 101.4))
    result = engine.manage(_bar(3, 101.4, 101.4, 100.9, 101.0))
    assert isinstance(result, TradeResult)
    assert result.reason == "stop_loss" and result.result == "win"
    assert result.exit_price == pytest.approx(101.5 * 0.997)


def test_short_side_mirrors_the_long():
    engine = _open("short")
    engine.manage(_bar(1, 100, 100.0, 99.3, 99.3))
    pos = engine.position
    assert pos.moved_to_breakeven and pos.stop_loss == pytest.approx(99.3 * 1.003)
    engine.manage(_bar(2, 99.3, 99.4, 98.5, 98.6))
    assert pos.stop_loss == pytest.approx(98.5 * 1.003)


def test_a_stop_can_only_be_tightened():
    engine = _open()
    pos = engine.position
    assert not engine._tighten_stop(pos, 99.0)            # further from price: refused
    assert pos.stop_loss == 99.5
    assert engine._tighten_stop(pos, 99.8) and pos.stop_loss == 99.8
    short = Position("BTCUSDT", "short", "s", 100, 100.5, 96, 1, 100, 10, 0, 8, 4.0)
    assert short.is_tighter(100.2) and not short.is_tighter(100.9)


def test_broker_refusal_leaves_the_stop_where_it_was(monkeypatch):
    engine = _open()
    monkeypatch.setattr(engine.broker, "update_stop", lambda *a, **k: False)
    engine.manage(_bar(1, 100, 100.7, 100.0, 100.7))
    assert engine.position.stop_loss == 99.5 and not engine.position.moved_to_breakeven


def test_profit_protection_numbers_come_from_config():
    engine = _open(profit_lock_trigger_pct=1.0, profit_trail_pct=0.5)
    engine.manage(_bar(1, 100, 100.7, 100.0, 100.7))      # under the 1.0% trigger
    assert engine.position.stop_loss == 99.5
    engine.manage(_bar(2, 100.7, 101.2, 100.6, 101.1))
    assert engine.position.stop_loss == pytest.approx(101.2 * 0.995)


# -- lessons ---------------------------------------------------------------- #
def _closed(journal, ts, result="loss", confluences=None, direction="long",
            reason="stop_loss", minutes=30, pnl=None, fees=0.1):
    confluences = confluences if confluences is not None else ["setup_match", "cvd", "volume"]
    pos = Position("BTCUSDT", direction, "liquidity_sweep_choch", 100, 99.5, 102, 1, 100,
                   10, ts - minutes * 60_000, 8, 4.0, confluences=confluences)
    pnl = pnl if pnl is not None else (2.0 if result == "win" else -0.6)
    journal.record(TradeResult(pos, 102 if result == "win" else 99.5, pnl, fees,
                               4.0 if result == "win" else -1.0, result, ts, reason),
                   confluences)


@pytest.fixture
def journal(tmp_path):
    return TradeJournal(str(tmp_path / "j.db"))


def test_every_loss_saves_a_lesson_and_wins_do_not(journal):
    _closed(journal, 1000 * 60_000)
    _closed(journal, 2000 * 60_000, result="win")
    lessons = journal.lessons()
    assert len(lessons) == 1
    row = lessons[0]
    assert row["setup_name"] == "liquidity_sweep_choch"
    assert row["reason_code"] == "against_15m_trend"      # no htf_alignment factor
    assert row["sentence"].endswith(".") and row["trade_id"]


def test_lesson_reason_codes():
    base = dict(setup_name="s", direction="long", exit_reason="stop_loss",
                pnl_usdt=-1.0, fees_usdt=0.1, minutes_open=30.0)
    aligned = ["setup_match", "htf_alignment"]
    assert derive_lesson(confluences=["setup_match"], **base).reason_code == "against_15m_trend"
    assert derive_lesson(confluences=aligned, **base).reason_code == "stopped_out"
    assert derive_lesson(confluences=aligned, **{**base, "minutes_open": 3}).reason_code \
        == "stopped_fast"
    assert derive_lesson(confluences=aligned, **{**base, "exit_reason": "max_duration"}) \
        .reason_code == "ran_out_of_time"
    assert derive_lesson(confluences=aligned, **{**base, "pnl_usdt": -0.05}).reason_code \
        == "fees_ate_profit"
    assert derive_lesson(confluences=aligned, **{**base, "pnl_usdt": 0.5}) is None


def test_jaccard():
    assert jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert jaccard(["a", "b"], ["a", "c"]) == pytest.approx(1 / 3)
    assert jaccard([], []) == 1.0


def _reviewer(journal):
    return LessonsReviewer(journal, similarity=0.75, min_matches=5, min_win_rate=0.2)


def test_five_losses_in_the_same_context_veto_the_next_entry(journal):
    ctx = ["setup_match", "cvd", "volume"]
    for i in range(5):
        _closed(journal, (i + 1) * _DAY, confluences=ctx)
    review = _reviewer(journal).review("liquidity_sweep_choch", "long", ctx, now_ts=10 * _DAY)
    assert review.veto and "against_15m_trend" in review.reason
    assert (review.matches, review.wins) == (5, 0)


def test_four_losses_are_not_enough_to_veto(journal):
    for i in range(4):
        _closed(journal, (i + 1) * _DAY)
    assert not _reviewer(journal).review(
        "liquidity_sweep_choch", "long", ["setup_match", "cvd", "volume"], 10 * _DAY).veto


def test_a_different_context_or_direction_is_not_vetoed(journal):
    for i in range(6):
        _closed(journal, (i + 1) * _DAY)
    r = _reviewer(journal)
    other = ["setup_match", "htf_alignment", "session", "funding"]
    assert not r.review("liquidity_sweep_choch", "long", other, 10 * _DAY).veto
    assert not r.review("liquidity_sweep_choch", "short", ["setup_match", "cvd", "volume"],
                        10 * _DAY).veto
    assert not r.review("value_area_absorption", "long", ["setup_match", "cvd", "volume"],
                        10 * _DAY).veto


def test_a_context_that_wins_often_enough_is_left_alone(journal):
    for i in range(4):
        _closed(journal, (i + 1) * _DAY)
    _closed(journal, 5 * _DAY, result="win")              # 1 of 5 = 20%, not under 20%
    assert not _reviewer(journal).review(
        "liquidity_sweep_choch", "long", ["setup_match", "cvd", "volume"], 10 * _DAY).veto


def test_reviewer_ignores_trades_that_closed_later(journal):
    for i in range(5):
        _closed(journal, (i + 20) * _DAY)                 # all after "now"
    assert not _reviewer(journal).review(
        "liquidity_sweep_choch", "long", ["setup_match", "cvd", "volume"], 10 * _DAY).veto


def test_learning_node_drops_the_intent_and_can_be_switched_off(journal):
    ctx = ["setup_match", "cvd", "volume"]
    for i in range(5):
        _closed(journal, (i + 1) * _DAY, confluences=ctx)
    state = {"intent": _intent(confluences=ctx), "candle": _bar(10 * 1440, 100, 101, 99, 100)}

    nodes = TradingNodes(Settings(mode=RunMode.SIM), journal, now_ts=10 * _DAY)
    out = nodes.learning_node(state)
    assert out["intent"] is None and "lessons veto" in out["lesson_veto"]

    off = TradingNodes(Settings(mode=RunMode.SIM, lessons_veto_enabled=False), journal,
                       now_ts=10 * _DAY)
    assert "intent" not in off.learning_node(state)       # intent passes through untouched


def test_learning_node_cannot_approve_what_risk_rejects(journal):
    """Passing the lessons check is not an approval: risk still screens."""
    nodes = TradingNodes(Settings(mode=RunMode.SIM), journal, now_ts=0)
    bad = _intent(sl=99.5, tp=100.5)                      # 1:1, far under the 1:4 minimum
    state = {"intent": bad, "candle": _bar(1, 100, 101, 99, 100)}
    state.update(nodes.learning_node(state))
    assert state["intent"] is bad
    assert not nodes.risk_node(state)["risk_decision"].approved
