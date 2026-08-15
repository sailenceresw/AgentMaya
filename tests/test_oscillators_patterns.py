"""Tests for the standard indicator suite and candlestick pattern detection.

Values are checked against hand calculations where the definition is
unambiguous, and against known-shape series otherwise.
"""
import pytest

from maya.data.models import Candle
from maya.signals import oscillators as osc
from maya.signals import patterns as pat


def _c(o: float, h: float, lo: float, c: float, v: float = 1.0, ts: int = 0) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=lo, close=c, volume=v)


# -- moving averages -------------------------------------------------------- #
def test_sma_hand_calculation():
    assert osc.sma([1, 2, 3, 4, 5], 5) == pytest.approx(3.0)
    assert osc.sma([1, 2, 3, 4, 5], 2) == pytest.approx(4.5)


def test_indicators_return_none_rather_than_a_short_window_value():
    """A 14-period RSI seeded from 4 bars is a different indicator, not a
    weaker one — returning it silently would trade on something unspecified."""
    assert osc.sma([1, 2], 5) is None
    assert osc.ema([1, 2], 5) is None
    assert osc.rsi([1, 2, 3], 14) is None
    assert osc.atr([_c(1, 2, 0, 1)], 14) is None
    assert osc.macd([1.0] * 10) is None
    assert osc.bollinger([1.0] * 5, 20) is None


def test_ema_reacts_faster_than_sma_to_a_step_up():
    """The point of an EMA: after a jump it moves toward the new level sooner
    than the equal-weighted average does."""
    closes = [100.0] * 30 + [110.0] * 5
    assert osc.ema(closes, 10) > osc.sma(closes, 10)


# -- volatility ------------------------------------------------------------- #
def test_true_range_uses_the_gap_when_price_jumps():
    prev = _c(10, 11, 9, 10)
    gapped = _c(20, 21, 19, 20)          # own range 2, but gapped 11 from prev close
    assert osc.true_range(gapped, prev) == pytest.approx(11.0)


def test_true_range_without_a_previous_candle_is_the_bar_range():
    assert osc.true_range(_c(10, 12, 8, 11), None) == pytest.approx(4.0)


def test_atr_of_constant_range_series_equals_that_range():
    candles = [_c(10, 11, 9, 10, ts=i) for i in range(30)]
    assert osc.atr(candles, 14) == pytest.approx(2.0)


# -- momentum --------------------------------------------------------------- #
def test_rsi_is_100_for_an_unbroken_advance():
    assert osc.rsi([float(i) for i in range(1, 30)], 14) == pytest.approx(100.0)


def test_rsi_is_zero_for_an_unbroken_decline():
    assert osc.rsi([float(i) for i in range(30, 1, -1)], 14) == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_neutral_not_a_divide_by_zero():
    assert osc.rsi([100.0] * 30, 14) == pytest.approx(50.0)


def test_rsi_sits_midrange_for_a_choppy_series():
    closes = [100 + (1 if i % 2 else -1) for i in range(40)]
    assert 30 < osc.rsi([float(c) for c in closes], 14) < 70


def test_macd_histogram_is_line_minus_signal():
    closes = [float(100 + i) for i in range(60)]
    line, signal, hist = osc.macd(closes)
    assert hist == pytest.approx(line - signal)


def test_macd_is_positive_in_an_uptrend_and_negative_in_a_downtrend():
    up = osc.macd([float(100 + i) for i in range(60)])
    down = osc.macd([float(160 - i) for i in range(60)])
    assert up[0] > 0
    assert down[0] < 0


# -- bands & vwap ----------------------------------------------------------- #
def test_bollinger_collapses_to_the_mean_with_no_variance():
    upper, mid, lower = osc.bollinger([100.0] * 20)
    assert upper == mid == lower == pytest.approx(100.0)


def test_bollinger_brackets_the_mean():
    closes = [100 + (5 if i % 2 else -5) for i in range(20)]
    upper, mid, lower = osc.bollinger([float(c) for c in closes])
    assert lower < mid < upper


def test_vwap_is_pulled_toward_the_high_volume_bar():
    light = _c(10, 10, 10, 10, v=1.0)
    heavy = _c(20, 20, 20, 20, v=99.0)
    assert osc.vwap([light, heavy]) > 19.0


def test_vwap_of_zero_volume_is_none_not_a_division_error():
    assert osc.vwap([_c(10, 10, 10, 10, v=0.0)]) is None


# -- supertrend ------------------------------------------------------------- #
def test_supertrend_follows_a_rising_market_from_below():
    candles = [_c(100 + i, 101 + i, 99 + i, 100.8 + i, ts=i) for i in range(40)]
    level, direction = osc.supertrend(candles)
    assert direction == "up"
    assert level < candles[-1].close      # trails beneath price


def test_supertrend_flips_and_sits_above_price_in_a_decline():
    candles = [_c(200 - i, 201 - i, 199 - i, 199.2 - i, ts=i) for i in range(40)]
    level, direction = osc.supertrend(candles)
    assert direction == "down"
    assert level > candles[-1].close


# -- candlestick patterns --------------------------------------------------- #
def test_doji_is_a_body_that_barely_exists():
    assert pat.is_doji(_c(100, 105, 95, 100.1))
    assert not pat.is_doji(_c(100, 105, 95, 104))


def test_hammer_needs_a_long_lower_wick_and_a_small_top():
    assert pat.is_hammer(_c(104, 105, 95, 104.5))
    assert not pat.is_hammer(_c(100, 110, 99, 101))   # upper wick too long


def test_shooting_star_is_the_hammer_inverted():
    assert pat.is_shooting_star(_c(96, 105, 95, 95.5))
    assert not pat.is_shooting_star(_c(104, 105, 95, 104.5))


def test_patterns_are_scale_invariant():
    """Same shape at $0.30 and at $65,000 must read identically."""
    small = _c(1.04, 1.05, 0.95, 1.045)
    large = _c(104000, 105000, 95000, 104500)
    assert pat.is_hammer(small) == pat.is_hammer(large) is True


def test_bullish_engulfing_requires_swallowing_the_prior_body():
    prev, cur = _c(100, 101, 97, 98), _c(97, 103, 96, 102)
    assert pat.is_bullish_engulfing(prev, cur)
    assert not pat.is_bearish_engulfing(prev, cur)


def test_bearish_engulfing_is_the_mirror():
    prev, cur = _c(98, 103, 97, 102), _c(103, 104, 96, 97)
    assert pat.is_bearish_engulfing(prev, cur)


def test_morning_star_is_detected_across_three_bars():
    a, b, c = _c(110, 111, 99, 100), _c(99, 100, 98, 99.5), _c(100, 108, 99, 107)
    assert pat.is_morning_star(a, b, c)
    assert not pat.is_evening_star(a, b, c)


def test_evening_star_is_detected_across_three_bars():
    a, b, c = _c(100, 111, 99, 110), _c(110, 111, 109, 110.2), _c(110, 111, 100, 101)
    assert pat.is_evening_star(a, b, c)


def test_inside_bar_is_contained_by_its_predecessor():
    assert pat.is_inside_bar(_c(100, 110, 90, 105), _c(102, 108, 95, 104))
    assert not pat.is_inside_bar(_c(100, 110, 90, 105), _c(102, 115, 95, 104))


def test_detect_patterns_reports_every_match_not_just_the_first():
    a, b, c = _c(110, 111, 99, 100), _c(99, 100, 98, 99.5), _c(100, 108, 99, 107)
    names = {m.name for m in pat.detect_patterns([a, b, c])}
    assert "morning_star" in names


def test_detect_patterns_is_safe_on_short_history():
    assert pat.detect_patterns([]) == []
    assert isinstance(pat.detect_patterns([_c(100, 101, 99, 100)]), list)


def test_pattern_bias_resolves_disagreement_to_neutral():
    """Two patterns pointing opposite ways is genuinely no information."""
    matches = [pat.PatternMatch("hammer", "bullish", 1),
               pat.PatternMatch("shooting_star", "bearish", 1)]
    assert pat.pattern_bias(matches) == "neutral"
    assert pat.pattern_bias(matches[:1]) == "bullish"
    assert pat.pattern_bias([]) == "neutral"
