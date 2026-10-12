"""Setups held to spec 6.2 — regressions from the code review."""
from __future__ import annotations

import pytest

from maya.data.models import Candle
from maya.signals import indicators as ind
from maya.signals.models import Absorption, SignalObject, VolumeProfile
from maya.signals.processor import COMPRESSION_LOOKBACK, SignalProcessor
from maya.strategy.setups import (
    RR_MIN_COMPRESSION_BREAKOUT,
    SetupContext,
    compression_breakout,
    value_area_absorption,
)

_CTX = SetupContext(swing_high_15m=110, swing_low_15m=95, range_pct=0.0)


def _tight_then(breakout: Candle, n=COMPRESSION_LOOKBACK + 5) -> SignalObject:
    """n quiet candles in a 0.08% range around 100, then ``breakout``."""
    proc = SignalProcessor("BTCUSDT")
    for i in range(n):
        proc.process(Candle(i * 60_000, 100.0, 100.04, 99.96, 100.0, 100.0,
                            taker_buy_volume=50.0))
    return proc.process(breakout)


def _bull_breakout(close=100.08, high=100.09, open_=100.0, vol=400.0, buy=330.0):
    return Candle(99 * 60_000, open_, high, 99.99, close, vol, taker_buy_volume=buy)


# -- compression range ----------------------------------------------------------- #
def test_range_excludes_the_current_candle():
    """The breakout bar must not widen the range it is breaking out of."""
    sig = _tight_then(Candle(99 * 60_000, 100, 105, 99, 104, 400, taker_buy_volume=300))
    assert sig.range_pct < 0.001
    assert sig.range_high == pytest.approx(100.04) and sig.range_low == pytest.approx(99.96)


def test_range_needs_the_full_lookback():
    proc = SignalProcessor("BTCUSDT")
    sig = None
    for i in range(COMPRESSION_LOOKBACK):     # one short of lookback + current
        sig = proc.process(Candle(i, 100, 100.04, 99.96, 100, 100))
    assert sig.range_high is None


# -- compression breakout ---------------------------------------------------------- #
def test_compression_breakout_fires_on_a_clean_breakout():
    """Regression: stop at mid-range with a 2x-range target capped R:R at
    1:4, so the setup required 1:5 and could never fire."""
    sig = _tight_then(_bull_breakout())
    intent = compression_breakout(sig, _CTX)
    assert intent is not None and intent.direction == "long"
    assert intent.stop_loss == pytest.approx(100.0)                 # mid-range
    assert intent.expected_rr >= RR_MIN_COMPRESSION_BREAKOUT
    assert intent.take_profit_1 - intent.entry_price >= 2 * (100.04 - 99.96)


def test_compression_breakout_needs_volume_cvd_and_a_full_body():
    assert compression_breakout(_tight_then(_bull_breakout(vol=150.0, buy=120)), _CTX) is None
    assert compression_breakout(_tight_then(_bull_breakout(buy=100.0)), _CTX) is None  # CVD < 0
    # Long upper wick: closes beyond the range but the body is under half the candle.
    assert compression_breakout(_tight_then(_bull_breakout(close=100.05, high=100.30)),
                                _CTX) is None


def test_compression_breakout_refuses_a_stop_over_half_a_percent():
    # A breakout that has already run 0.8% puts mid-range too far below entry.
    far = _bull_breakout(close=100.8, high=100.81, open_=100.0, vol=400, buy=330)
    assert compression_breakout(_tight_then(far), _CTX) is None


# -- value area absorption ----------------------------------------------------------- #
def test_value_area_exports_the_tp1_rr_it_actually_trades():
    sig = SignalObject(timestamp=0, symbol="BTCUSDT", price=100.0, high=100.5, low=99.5,
                       atr=0.2)
    sig.volume_profile = VolumeProfile(vah=110, poc=105, val=100, shape="D")
    sig.absorption = Absorption(detected=True, side="bullish", level=100.0, strength=0.8)
    intent = value_area_absorption(sig, _CTX)
    assert intent is not None
    tp1_rr = (intent.take_profit_1 - intent.entry_price) / (intent.entry_price - intent.stop_loss)
    assert intent.expected_rr == pytest.approx(tp1_rr)


# -- absorption ---------------------------------------------------------------------- #
def test_absorption_picks_the_densest_wick_cluster():
    """Overlapping wick levels: starting from 98.00 finds a 3-wick cluster
    (98.00/98.10/98.12), but the cluster around 98.12 holds all 4
    (98.00-98.25 within the 0.15% tolerance). The first-found rule reported
    the thinner one; the densest is the level price defended most."""
    def wick(ts, low):
        return Candle(ts, 100.0, 100.05, low, 99.95, 300.0)

    quiet = [Candle(t, 100.0, 100.05, 99.95, 100.0, 50.0) for t in (5, 6)]
    candles = [wick(1, 98.00), wick(2, 98.10), wick(3, 98.12), wick(4, 98.25)] + quiet
    ab = ind.detect_absorption(candles)
    assert ab.detected and ab.side == "bullish"
    assert ab.level == pytest.approx((98.00 + 98.10 + 98.12 + 98.25) / 4)


def test_compression_breakout_is_off_unless_enabled():
    """It has never been measured (it could not fire before) and uses the
    CVD sign the 24-month study found inverted, so it ships disabled."""
    from maya.config import Settings
    from maya.strategy.engine import StrategyEngine

    assert compression_breakout not in StrategyEngine(Settings())._setups
    assert compression_breakout in StrategyEngine(
        Settings(compression_breakout_enabled=True))._setups
