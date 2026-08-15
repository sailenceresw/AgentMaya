"""Unit tests for the order-flow indicators (spec section 5.2)."""
import pytest

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


# -- real order flow -------------------------------------------------------- #
# CVD used to be computed from *synthetic* ticks reconstructed out of candle
# direction, which made every CVD-derived signal a function of the generator.
# Binance's kline field 9 (taker buy base volume) gives the true split.
from maya.signals.indicators import delta_for


def test_delta_uses_the_exchange_taker_buy_volume():
    # 100 traded, 70 of it aggressive buys => delta = 70 - 30 = +40
    c = Candle(timestamp=0, open=1, high=2, low=0, close=1, volume=100.0,
                taker_buy_volume=70.0)
    assert c.delta == pytest.approx(40.0)
    assert delta_for(c) == pytest.approx(40.0)


def test_delta_is_negative_when_sellers_dominate():
    c = Candle(timestamp=0, open=1, high=2, low=0, close=1, volume=100.0,
                taker_buy_volume=20.0)
    assert delta_for(c) == pytest.approx(-60.0)


def test_delta_is_none_without_the_exchange_figure():
    """None, not 0.0 — so callers can fall back instead of silently reporting
    a flat delta for a bar that simply lacked the column."""
    assert Candle(timestamp=0, open=1, high=2, low=0, close=1,
                   volume=100.0).delta is None


def test_delta_for_falls_back_to_ticks_when_taker_volume_absent():
    trades = [Trade(timestamp=0, price=1.0, qty=3.0, is_buyer_maker=False),  # buy
              Trade(timestamp=0, price=1.0, qty=1.0, is_buyer_maker=True)]   # sell
    c = Candle(timestamp=0, open=1, high=2, low=0, close=1, volume=4.0,
                trades=trades)
    assert delta_for(c) == pytest.approx(2.0)


def test_exchange_figure_wins_over_ticks_when_both_present():
    """A partial tick sample must never override the exchange's own total."""
    trades = [Trade(timestamp=0, price=1.0, qty=1.0, is_buyer_maker=True)]
    c = Candle(timestamp=0, open=1, high=2, low=0, close=1, volume=100.0,
                taker_buy_volume=70.0, trades=trades)
    assert delta_for(c) == pytest.approx(40.0)
