"""Flipped-CVD setups: same geometry, delta confirmation reversed."""
from __future__ import annotations

import pytest

from maya.config import Settings
from maya.data.models import Candle
from maya.research.hypotheses import CATALOG
from maya.signals.models import SignalObject
from maya.signals.processor import COMPRESSION_LOOKBACK, SignalProcessor
from maya.strategy.confluence import score_confluence
from maya.strategy.engine import StrategyEngine
from maya.strategy.setups import (
    CVD_FLIP_SETUPS,
    SetupContext,
    compression_breakout,
    compression_breakout_cvd_flip,
    liquidity_sweep_choch,
    liquidity_sweep_choch_cvd_flip,
)


def _sweep_sig(cvd):
    # A high sweep: wick above the prior swing, close back under it, on a volume spike.
    return SignalObject(timestamp=0, symbol="BTCUSDT", price=100.0, high=100.6, low=99.9,
                        cvd_1m=cvd, volume=300.0, avg_volume=100.0, atr=0.1)


_HIGH_SWEEP = SetupContext(swing_high_15m=100.5, swing_low_15m=96.0, range_pct=0.01,
                           swept_high=True)
_LOW_SWEEP = SetupContext(swing_high_15m=104.0, swing_low_15m=99.5, range_pct=0.01,
                          swept_low=True)


def test_original_sweep_short_wants_negative_delta_and_the_flip_wants_positive():
    assert liquidity_sweep_choch(_sweep_sig(-5), _HIGH_SWEEP).direction == "short"
    assert liquidity_sweep_choch(_sweep_sig(+5), _HIGH_SWEEP) is None
    assert liquidity_sweep_choch_cvd_flip(_sweep_sig(-5), _HIGH_SWEEP) is None
    flipped = liquidity_sweep_choch_cvd_flip(_sweep_sig(+5), _HIGH_SWEEP)
    assert flipped.direction == "short"
    assert flipped.setup_name == "liquidity_sweep_choch_cvd_flip"


def test_flip_keeps_the_geometry_of_the_original():
    sig_low = SignalObject(timestamp=0, symbol="BTCUSDT", price=100.0, high=100.1, low=99.4,
                           cvd_1m=5, volume=300.0, avg_volume=100.0, atr=0.1)
    original = liquidity_sweep_choch(sig_low, _LOW_SWEEP)
    sig_low.cvd_1m = -5
    flipped = liquidity_sweep_choch_cvd_flip(sig_low, _LOW_SWEEP)
    assert original.direction == flipped.direction == "long"
    assert (original.entry_price, original.stop_loss, original.take_profit_1) == \
        (flipped.entry_price, flipped.stop_loss, flipped.take_profit_1)


def _breakout(buy):
    proc = SignalProcessor("BTCUSDT")
    for i in range(COMPRESSION_LOOKBACK + 5):
        proc.process(Candle(i * 60_000, 100.0, 100.04, 99.96, 100.0, 100.0,
                            taker_buy_volume=50.0))
    return proc.process(Candle(99 * 60_000, 100.0, 100.09, 99.99, 100.08, 400.0,
                               taker_buy_volume=buy))


def test_compression_flip_takes_the_breakout_only_against_the_delta():
    ctx = SetupContext(0, 0, 0)
    with_delta, against_delta = _breakout(330.0), _breakout(70.0)
    assert compression_breakout(with_delta, ctx).direction == "long"
    assert compression_breakout_cvd_flip(with_delta, ctx) is None
    assert compression_breakout(against_delta, ctx) is None
    flipped = compression_breakout_cvd_flip(against_delta, ctx)
    assert flipped.direction == "long" and flipped.setup_name.endswith("_cvd_flip")


def test_confluence_scores_cvd_on_the_flipped_reading():
    sig = _sweep_sig(+5)
    original_name = liquidity_sweep_choch(_sweep_sig(-5), _HIGH_SWEEP)   # short, cvd < 0
    flipped = liquidity_sweep_choch_cvd_flip(sig, _HIGH_SWEEP)           # short, cvd > 0
    assert "cvd" in score_confluence(flipped, sig)[1]
    assert "cvd" not in score_confluence(original_name, sig)[1]


def test_flipped_setups_are_off_unless_enabled():
    default = StrategyEngine(Settings())._setups
    assert not set(CVD_FLIP_SETUPS) & set(default)
    assert liquidity_sweep_choch in default                 # originals untouched
    on = StrategyEngine(Settings(cvd_flip_setups_enabled=True))._setups
    assert set(CVD_FLIP_SETUPS) <= set(on) and liquidity_sweep_choch in on


def test_setups_only_isolates_named_setups_and_rejects_typos():
    only = StrategyEngine(Settings(setups_only="liquidity_sweep_choch_cvd_flip"))._setups
    assert only == (liquidity_sweep_choch_cvd_flip,)
    with pytest.raises(ValueError, match="unknown setups"):
        StrategyEngine(Settings(setups_only="liquidty_sweep"))


def test_cvd_flip_hypothesis_is_declared_on_15m_and_runnable():
    hyp = CATALOG["cvd_flip"]
    assert hyp.timeframe == "15m" and hyp.cost == "taker" and not hyp.blocked
