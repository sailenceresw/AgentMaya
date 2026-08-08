"""Tests for the journal-backed risk state restoration (spec section 8.4) and
the candle-boundary scheduler."""
from maya.config import RunMode, Settings
from maya.data.scheduler import CandleScheduler
from maya.execution.engine import TradeResult
from maya.execution.models import Position
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager, SystemState, RiskLimits

DAY_MS = 86_400_000


def _settings():
    return Settings(mode=RunMode.SIM, start_equity=1000)


def _pos(**overrides):
    base = dict(symbol="BTCUSDT", direction="long", setup_name="test",
                entry_price=100, stop_loss=95, take_profit=120,
                quantity=1, notional=100, leverage=5, opened_ts=0,
                confluence_score=8, rr_planned=4.0)
    base.update(overrides)
    return Position(**base)


def _result(exit_ts: int, pnl: float, result: str):
    return TradeResult(
        position=_pos(), exit_price=100, pnl_usdt=pnl, fees_usdt=0.5,
        rr_realized=1.0, result=result, exit_ts=exit_ts, reason="take_profit",
    )


def test_fresh_journal_gives_zeroed_state(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    rm = RiskManager.from_journal(_settings(), 1000, journal, now_ts=10 * DAY_MS)
    assert rm.account.trades_today == 0
    assert rm.account.consecutive_losses == 0
    assert rm.account.day_start_equity == 1000
    journal.close()


def test_trades_today_only_counts_current_utc_day(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_result(exit_ts=9 * DAY_MS + 1000, pnl=10, result="win"), [])   # yesterday
    journal.record(_result(exit_ts=10 * DAY_MS + 1000, pnl=-5, result="loss"), [])  # today
    now_ts = 10 * DAY_MS + 5000
    assert journal.trades_today(now_ts) == 1
    assert journal.pnl_today_usdt(now_ts) == -5
    journal.close()


def test_consecutive_losses_survives_restart(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_result(exit_ts=1000, pnl=-5, result="loss"), [])
    journal.record(_result(exit_ts=2000, pnl=-5, result="loss"), [])
    # A brand-new RiskManager (simulating a process restart) must inherit the
    # streak, not start clean at zero.
    rm = RiskManager.from_journal(_settings(), 990, journal, now_ts=3000)
    assert rm.account.consecutive_losses == 2
    assert rm.account.consecutive_losses >= RiskLimits.MAX_CONSECUTIVE_LOSSES
    assert rm.account.state is SystemState.COOLDOWN
    journal.close()


def test_a_win_resets_the_streak_even_after_restart(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_result(exit_ts=1000, pnl=-5, result="loss"), [])
    journal.record(_result(exit_ts=2000, pnl=10, result="win"), [])
    rm = RiskManager.from_journal(_settings(), 1005, journal, now_ts=3000)
    assert rm.account.consecutive_losses == 0
    assert rm.account.state is SystemState.RUNNING
    journal.close()


def test_day_start_equity_backs_out_todays_pnl(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_result(exit_ts=10 * DAY_MS + 1000, pnl=25, result="win"), [])
    now_ts = 10 * DAY_MS + 5000
    rm = RiskManager.from_journal(_settings(), 1025, journal, now_ts=now_ts)
    assert abs(rm.account.day_start_equity - 1000) < 1e-6
    journal.close()


def test_journal_read_failure_falls_back_to_zero_not_a_crash(tmp_path):
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.close()  # closed connection -> session_state() will raise
    rm = RiskManager.from_journal(_settings(), 1000, journal, now_ts=10 * DAY_MS)
    assert rm.account.trades_today == 0
    assert rm.account.consecutive_losses == 0


# -- candle scheduler ---------------------------------------------------------- #

def test_scheduler_finds_next_boundary():
    sched = CandleScheduler("1m", close_buffer_ms=0)
    # 90_500ms -> next 60s boundary is 120_000ms
    assert sched.next_boundary_ms(90_500) == 120_000


def test_scheduler_on_exact_boundary_returns_next_one():
    sched = CandleScheduler("1m", close_buffer_ms=0)
    assert sched.next_boundary_ms(60_000) == 120_000


def test_scheduler_ms_until_next_close_includes_buffer():
    sched = CandleScheduler("1m", close_buffer_ms=1_500)
    assert sched.ms_until_next_close(119_000) == 1_000 + 1_500


def test_scheduler_backoff_increases_and_caps(monkeypatch):
    import maya.data.scheduler as scheduler_mod
    monkeypatch.setattr(scheduler_mod.time, "sleep", lambda _s: None)

    sched = CandleScheduler("1m", max_backoff_s=4.0)
    assert sched._backoff_s == 1.0
    sched.on_error(RuntimeError("boom"))
    assert sched._backoff_s == 2.0
    sched.on_error(RuntimeError("boom"))
    assert sched._backoff_s == 4.0
    sched.on_error(RuntimeError("boom"))
    assert sched._backoff_s == 4.0  # capped
    sched.on_success()
    assert sched._backoff_s == 1.0
