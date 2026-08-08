"""Unit tests for the order-flow indicators (spec section 5.2)."""
from maya.data.models import Candle, Trade
from maya.signals import indicators as ind


def test_candle_delta_buy_heavy():
    trades = [
        Trade(0, 100, 5, is_buyer_maker=False),  # aggressive buy
        Trade(0, 100, 2, is_buyer_maker=True),   # aggressive sell
    ]
    assert ind.candle_delta(trades) == 3


def test_volume_profile_poc_within_range():
    candles = [Candle(i, 100, 105, 95, 101, 100) for i in range(20)]
    vp = ind.volume_profile(candles)
    assert vp.val <= vp.poc <= vp.vah
    assert 95 <= vp.poc <= 105


def test_cvd_divergence_bullish():
    prices = [100, 99, 98, 97, 96, 95]   # lower lows
    cvds = [0, 1, 2, 3, 4, 5]            # higher
    assert ind.cvd_divergence(prices, cvds) == "bullish"


def test_absorption_needs_clustered_wicks():
    # flat candles => no absorption
    candles = [Candle(i, 100, 100.5, 99.5, 100, 50) for i in range(6)]
    assert ind.detect_absorption(candles).detected is False
