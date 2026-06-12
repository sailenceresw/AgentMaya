"""Regression tests for the bug-hunt fixes."""
from maya.config import RunMode, Settings
from maya.execution.models import Position
from maya.execution.engine import TradeResult
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager, SystemState
from maya.signals.models import SignalObject, VolumeProfile, Absorption
from maya.signals.processor import SignalProcessor
from maya.data.models import Candle
from maya.strategy.models import TradeIntent
from maya.strategy.setups import SetupContext, value_area_absorption


def _settings():
    return Settings(mode=RunMode.SIM, start_equity=1000)


def _intent(rr=5.0):
    return TradeIntent("BTCUSDT", "long", "value_area_absorption", 100, 95, 120,
                       125, confluence_score=8, expected_rr=rr)


# --- Bug 1: cooldown must reset the loss streak when it elapses --------------
def test_cooldown_resets_streak_after_elapse():
    rm = RiskManager(_settings(), 1000)
    rm.roll_periods(0)
    for _ in range(2):  # two losses -> cooldown
        rm.on_trade_opened()
        rm.on_trade_closed(-10, 0)
    assert rm.account.state is SystemState.COOLDOWN
    assert rm.account.consecutive_losses == 2

    later = rm.account.cooldown_until_ts + 1
    decision = rm.screen(_intent(), later)
    assert decision.approved is True
    assert rm.account.consecutive_losses == 0  # streak cleared, no perpetual loop


# --- Bug 5: an active cooldown must survive a day rollover -------------------
def test_cooldown_survives_day_rollover():
    rm = RiskManager(_settings(), 1000)
    t = 86_400_000 - 600_000  # 23:50 on day 0
    rm.roll_periods(t)
    for _ in range(2):
        rm.on_trade_opened()
        rm.on_trade_closed(-10, t)
    assert rm.account.state is SystemState.COOLDOWN
    cooldown_until = rm.account.cooldown_until_ts

    next_day = 86_400_000 + 1  # rolls into day 1, still before cooldown expiry
    assert next_day < cooldown_until
    rm.roll_periods(next_day)
    decision = rm.screen(_intent(), next_day)
    assert decision.approved is False
    assert decision.reason == "in cooldown"


# --- Bug 4: value_area_absorption exports TP1-based R:R, not TP2 -------------
def test_value_area_rr_is_tp1_based():
    sig = SignalObject(timestamp=0, symbol="BTCUSDT", price=100.0, high=100.5, low=99.5)
    sig.volume_profile = VolumeProfile(vah=110, poc=105, val=100, shape="D")
    sig.absorption = Absorption(detected=True, side="bullish", level=100.0, strength=0.8)
    ctx = SetupContext(swing_high_15m=110, swing_low_15m=95, range_pct=0.05,
                       swept_high=False, swept_low=False,
                       breakout_up=False, breakout_down=False)
    intent = value_area_absorption(sig, ctx)
    assert intent is not None
    entry, sl, tp1, tp2 = intent.entry_price, intent.stop_loss, intent.take_profit_1, intent.take_profit_2
    rr_tp1 = abs(tp1 - entry) / abs(entry - sl)
    rr_tp2 = abs(tp2 - entry) / abs(entry - sl)
    assert abs(intent.expected_rr - rr_tp1) < 1e-6
    assert abs(intent.expected_rr - rr_tp2) > 1.0  # definitely not the TP2 number


# --- Bug 6: compression range is a short-window measure ----------------------
def test_range_pct_is_short_window():
    proc = SignalProcessor("BTCUSDT")
    for i in range(10):  # wide early candles
        proc.process(Candle(i, 100, 200, 50, 100, 100))
    sig = None
    for i in range(10, 45):  # 35 tight candles
        sig = proc.process(Candle(i, 100, 100.1, 99.9, 100, 100))
    # Despite the earlier wide candles, the recent window is tight.
    assert sig.range_pct < 0.01


# --- Bug 6b: compression_breakout is reachable on a clean breakout -----------
def test_compression_breakout_can_fire():
    from maya.data.models import Trade
    from maya.strategy.engine import StrategyEngine
    from maya.strategy import setups as S

    proc = SignalProcessor("BTCUSDT")
    eng = StrategyEngine(_settings())
    for i in range(35):  # tight ~0.08% range
        sig = proc.process(Candle(i * 60000, 100.00, 100.04, 99.96, 100.00, 100))
        eng.observe(sig)
    ts = 36 * 60000
    breakout = Candle(ts, 100.00, 100.07, 100.00, 100.05, 400,
                      [Trade(ts, 100.05, 330, False), Trade(ts, 100.0, 70, True)])
    sig = proc.process(breakout)
    ctx = eng.observe(sig)
    intent = S.compression_breakout(sig, ctx)
    assert intent is not None
    assert intent.expected_rr >= 5.0


# --- Bug 8: profit_factor is finite when there are no losses -----------------
def test_profit_factor_capped_without_losses(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "j.db"))
    pos = Position("BTCUSDT", "long", "x", 100, 95, 120, quantity=0.1, notional=10,
                   leverage=5, opened_ts=0, confluence_score=8, rr_planned=5.0)
    j.record(TradeResult(pos, 120.0, pnl_usdt=10.0, fees_usdt=0.1, rr_realized=5.0,
                         result="win", exit_ts=1, reason="take_profit"), [])
    pf = j.metrics()["profit_factor"]
    assert pf == 999.99 and pf != float("inf")
    j.close()
