"""Tests for higher-timeframe aggregation and chart reading.

The properties that matter most here are the ones that make multi-timeframe
analysis trustworthy rather than merely present: epoch-aligned buckets, and no
lookahead onto a bar that has not closed.
"""
import pytest

from maya.data.models import Candle
from maya.signals import technicals as ta
from maya.signals.models import HTFContext
from maya.signals.processor import SignalProcessor
from maya.signals.timeframes import TimeframeAggregator, parse_timeframe

MIN = 60_000


def _c(ts_min: int, o: float, h: float, lo: float, c: float, v: float = 1.0) -> Candle:
    return Candle(timestamp=ts_min * MIN, open=o, high=h, low=lo, close=c, volume=v)


def _series(closes: list[float], start_min: int = 0) -> list[Candle]:
    return [_c(start_min + i, c, c + 0.5, c - 0.5, c) for i, c in enumerate(closes)]


# -- timeframe parsing ------------------------------------------------------ #
def test_parse_timeframe_units():
    assert parse_timeframe("15m") == 15
    assert parse_timeframe("1h") == 60
    assert parse_timeframe("4h") == 240
    assert parse_timeframe("1d") == 1440


@pytest.mark.parametrize("bad", ["", "15", "m15", "0m", "abc", "-5m"])
def test_parse_timeframe_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_timeframe(bad)


# -- aggregation ------------------------------------------------------------ #
def test_aggregator_folds_ohlcv_correctly():
    agg = TimeframeAggregator(5)
    for c in [_c(0, 10, 12, 9, 11, 1.0), _c(1, 11, 15, 10, 14, 2.0),
              _c(2, 14, 14, 8, 9, 3.0), _c(3, 9, 11, 9, 10, 4.0),
              _c(4, 10, 13, 10, 12, 5.0)]:
        assert agg.update(c) is None  # bucket still filling

    closed = agg.update(_c(5, 12, 12, 12, 12, 1.0))  # next bucket closes it
    assert closed is not None
    assert closed.open == 10        # first candle's open
    assert closed.high == 15        # max of highs
    assert closed.low == 8          # min of lows
    assert closed.close == 12       # last candle's close
    assert closed.volume == 15.0    # sum


def test_buckets_are_epoch_aligned_not_start_aligned():
    """A 15m bar starts at :00/:15/:30/:45 regardless of when we start
    listening — otherwise our bars disagree with every chart on earth."""
    agg = TimeframeAggregator(15)
    agg.update(_c(7, 1, 1, 1, 1))    # start mid-bucket
    agg.update(_c(14, 2, 2, 2, 2))
    closed = agg.update(_c(15, 3, 3, 3, 3))
    assert closed is not None
    assert closed.timestamp == 0     # the 00:00-00:15 bucket, not 00:07


def test_forming_candle_is_never_published():
    """No lookahead: a bar is only visible after a later bar proves it closed."""
    agg = TimeframeAggregator(15)
    for m in range(14):
        assert agg.update(_c(m, 1, 1, 1, 1)) is None
    assert len(agg.closed) == 0
    assert agg.forming is not None   # tracked, but not handed out


# -- indicators ------------------------------------------------------------- #
def test_ema_needs_a_full_window():
    assert ta.ema([1.0, 2.0], 9) is None
    assert ta.ema([1.0] * 9, 9) == pytest.approx(1.0)


def test_ema_tracks_a_rising_series_below_the_last_price():
    values = [float(i) for i in range(1, 31)]
    e = ta.ema(values, 9)
    assert e is not None
    assert e < values[-1]        # lags a rising series
    assert e > values[-9]        # but weights recent values most


def _zigzag(start: float, legs: int, up: int, down: int, step: float = 1.0) -> list[float]:
    """Trending series *with pullbacks* — the only shape that has swings."""
    closes = [start]
    for _ in range(legs):
        for _ in range(up):
            closes.append(closes[-1] + step)
        for _ in range(down):
            closes.append(closes[-1] - step)
    return closes


def test_trend_up_when_fast_ema_leads():
    ctx = ta.analyze(_series([100 + i for i in range(40)]), "15m")
    assert ctx.trend == "up"


def test_trend_down_when_fast_ema_trails():
    ctx = ta.analyze(_series([100 - i for i in range(40)]), "15m")
    assert ctx.trend == "down"


def test_a_pure_ramp_has_no_confirmed_swings():
    """Not a gap in the detector — a series that never pulls back genuinely
    has no swing high or low, and claiming structure from it would be
    inventing pivots that price never made."""
    assert ta.analyze(_series([100 + i for i in range(40)]), "15m").structure == "unknown"


def test_structure_reads_higher_highs_on_a_rising_zigzag():
    ctx = ta.analyze(_series(_zigzag(100.0, legs=6, up=6, down=3)), "15m")
    assert ctx.trend == "up"
    assert ctx.structure == "higher_highs"


def test_structure_reads_lower_lows_on_a_falling_zigzag():
    ctx = ta.analyze(_series(_zigzag(200.0, legs=6, up=3, down=6)), "15m")
    assert ctx.trend == "down"
    assert ctx.structure == "lower_lows"


def test_structure_reads_range_when_swings_make_no_progress():
    ctx = ta.analyze(_series(_zigzag(100.0, legs=6, up=4, down=4)), "15m")
    assert ctx.structure == "range"


def test_flat_market_is_neutral_not_a_coin_flip():
    """Without a band, two EMAs sitting on top of each other flip the trend
    every bar and the factor becomes noise."""
    ctx = ta.analyze(_series([100.0] * 40), "15m")
    assert ctx.trend == "neutral"


def test_analyze_on_empty_series_is_safe():
    ctx = ta.analyze([], "1h")
    assert ctx.trend == "unknown"
    assert ctx.candles == 0


def test_support_and_resistance_bracket_price():
    closes = [100, 102, 104, 101, 98, 96, 99, 103, 106, 104,
              101, 99, 97, 100, 102, 105, 103, 100, 98, 101]
    ctx = ta.analyze(_series([float(c) for c in closes]), "15m")
    if ctx.support is not None:
        assert ctx.support < ctx.price
    if ctx.resistance is not None:
        assert ctx.resistance > ctx.price


# -- alignment -------------------------------------------------------------- #
def test_alignment_requires_every_timeframe_to_agree():
    up = HTFContext(timeframe="15m", trend="up")
    down = HTFContext(timeframe="1h", trend="down")
    assert ta.aligned({"15m": up}, "long")
    assert not ta.aligned({"15m": up, "1h": down}, "long")
    assert not ta.aligned({"15m": up, "1h": down}, "short")


def test_neutral_timeframe_blocks_alignment():
    assert not ta.aligned({"15m": HTFContext(timeframe="15m", trend="neutral")}, "long")


def test_no_htf_data_is_not_alignment():
    """Before the first HTF bar closes, absence of evidence must not score."""
    assert not ta.aligned({}, "long")


# -- processor integration -------------------------------------------------- #
def test_processor_populates_htf_after_enough_candles():
    proc = SignalProcessor("BTCUSDT", htf_timeframes=("15m",))
    sig = None
    for m in range(120):
        price = 100.0 + m * 0.1
        sig = proc.process(_c(m, price, price + 0.2, price - 0.2, price, 10.0))
    assert "15m" in sig.htf
    assert sig.htf["15m"].candles >= 6


def test_processor_htf_is_empty_before_the_first_bar_closes():
    proc = SignalProcessor("BTCUSDT", htf_timeframes=("1h",))
    sig = proc.process(_c(0, 100, 101, 99, 100, 5.0))
    assert sig.htf == {}


def test_htf_can_be_disabled():
    proc = SignalProcessor("BTCUSDT", htf_timeframes=())
    for m in range(200):
        sig = proc.process(_c(m, 100, 101, 99, 100, 5.0))
    assert sig.htf == {}


# -- liquidity targeting (liquidation_cluster proxy) ------------------------ #
def test_long_targeting_above_resistance_is_aiming_at_liquidity():
    ctx = {"15m": HTFContext(timeframe="15m", resistance=105.0, support=95.0)}
    assert ta.targets_liquidity(ctx, "long", entry=100.0, target=106.0)


def test_long_targeting_short_of_resistance_is_not():
    ctx = {"15m": HTFContext(timeframe="15m", resistance=105.0, support=95.0)}
    assert not ta.targets_liquidity(ctx, "long", entry=100.0, target=101.0)


def test_short_targeting_below_support_is_aiming_at_liquidity():
    ctx = {"15m": HTFContext(timeframe="15m", resistance=105.0, support=95.0)}
    assert ta.targets_liquidity(ctx, "short", entry=100.0, target=94.0)


def test_a_target_just_shy_of_the_level_still_counts():
    """Stops cluster *around* a level, not exactly on it."""
    ctx = {"15m": HTFContext(timeframe="15m", resistance=105.0)}
    assert ta.targets_liquidity(ctx, "long", entry=100.0, target=104.95)


def test_no_levels_means_no_liquidity_claim():
    assert not ta.targets_liquidity({}, "long", entry=100.0, target=110.0)
    assert not ta.targets_liquidity({"1h": HTFContext(timeframe="1h")},
                                    "long", entry=100.0, target=110.0)


# -- htf selection relative to the entry timeframe -------------------------- #
def test_htf_list_drops_timeframes_at_or_below_the_entry_timeframe():
    """A 'higher' timeframe equal to the entry timeframe is the same series,
    so alignment would confirm the entry bar with itself and score for free."""
    from maya.config import Settings

    s = Settings(timeframe="15m", htf_timeframes="15m,1h")
    assert s.htf_list == ["1h"]


def test_htf_list_steps_up_when_everything_configured_is_too_low():
    from maya.config import Settings

    s = Settings(timeframe="1h", htf_timeframes="15m")
    assert s.htf_list == ["4h", "16h"]


def test_htf_list_keeps_the_defaults_on_a_1m_entry():
    from maya.config import Settings

    assert Settings(timeframe="1m").htf_list == ["15m", "1h"]


def test_blank_htf_config_stays_disabled():
    from maya.config import Settings

    assert Settings(timeframe="15m", htf_timeframes="").htf_list == []


def test_unparseable_htf_entries_are_ignored_not_fatal():
    from maya.config import Settings

    assert Settings(timeframe="1m", htf_timeframes="bogus,1h").htf_list == ["1h"]
