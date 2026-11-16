"""Kronos input: signal rule, cache, forecaster and the Iris hookup.

No model is downloaded. ``_FakePredictor`` stands in for the Kronos repo's
``KronosPredictor`` and returns an invented forecast.
"""
from __future__ import annotations

import pandas as pd
import pytest

from maya.config import RunMode, Settings
from maya.data.models import Candle
from maya.research import __main__ as cli
from maya.research.hypotheses import CATALOG, KRONOS_STRIDE, kronos_samples
from maya.signals.kronos_signal import (
    ForecastCache,
    KronosForecaster,
    load_predictor,
    signal_rule,
    unavailable_reason,
)
from maya.signals.processor import SignalProcessor

M15 = 900_000
RT = 0.0010      # 10 bps round trip


class _FakePredictor:
    """Forecasts the last close times ``factor`` and records what it was asked."""

    def __init__(self, factor=1.01):
        self.factor, self.calls = factor, []

    def predict(self, df, x_timestamp, y_timestamp, pred_len, T, top_p, sample_count):
        self.calls.append(dict(rows=len(df), pred_len=pred_len, T=T, top_p=top_p,
                               sample_count=sample_count, y=list(y_timestamp),
                               columns=list(df.columns)))
        close = float(df["close"].iloc[-1]) * self.factor
        return pd.DataFrame({"close": [close] * pred_len}, index=y_timestamp)


def _bars(n, start=100.0, step=0.0):
    return [Candle(i * M15, start + i * step, start + i * step + 1, start + i * step - 1,
                   start + i * step, 10.0) for i in range(n)]


def _forecaster(predictor, cache=None, **kw):
    kw.setdefault("lookback", 8)
    return KronosForecaster("BTCUSDT", M15, RT, predictor=predictor, cache=cache, **kw)


# -- signal rule ------------------------------------------------------------ #
@pytest.mark.parametrize("forecast,expected", [
    (100.20, "long"),       # +20 bps clears 10 bps cost + 5 bps margin
    (100.14, None),         # just inside the 15 bps band
    (100.10, None),         # would not pay for its own trade
    (99.80, "short"),
    (99.90, None),
    (100.00, None),
])
def test_signal_needs_the_forecast_to_clear_cost_plus_margin(forecast, expected):
    assert signal_rule(100.0, forecast, RT, 0.0005) == expected


def test_signal_rule_rejects_nonsense_prices():
    assert signal_rule(0, 100, RT, 0) is None
    assert signal_rule(100, -1, RT, 0) is None


# -- forecaster ------------------------------------------------------------- #
def test_forecaster_feeds_the_newest_lookback_bars_with_fixed_sampling():
    fake = _FakePredictor()
    fc = _forecaster(fake, lookback=8, pred_len=4, samples=3, temperature=1.0, top_p=0.9)
    sig = fc.signal(_bars(20, step=1.0))
    call = fake.calls[0]
    assert call["rows"] == 8 and call["pred_len"] == 4
    assert (call["T"], call["top_p"], call["sample_count"]) == (1.0, 0.9, 3)
    assert call["columns"] == ["open", "high", "low", "close", "volume"]
    # The forecast timestamps continue the series at the bar interval.
    assert call["y"][0] == pd.Timestamp(20 * M15, unit="ms")
    assert sig.direction == "long" and sig.horizon == 4
    assert sig.forecast_close == pytest.approx(119.0 * 1.01)
    assert sig.expected_return == pytest.approx(0.01)


def test_too_little_history_gives_no_signal_and_no_model_call():
    fake = _FakePredictor()
    assert _forecaster(fake, lookback=8).signal(_bars(5)) is None
    assert fake.calls == []


def test_flat_forecast_is_a_reading_with_no_direction():
    sig = _forecaster(_FakePredictor(factor=1.0005)).signal(_bars(10))
    assert sig is not None and sig.direction is None


def test_lookback_over_the_model_context_is_refused():
    with pytest.raises(ValueError):
        _forecaster(_FakePredictor(), lookback=513)


# -- cache ------------------------------------------------------------------ #
def test_cache_saves_the_model_call_on_a_repeat(tmp_path):
    cache = ForecastCache(str(tmp_path / "k" / "cache.db"))
    fake = _FakePredictor()
    fc = _forecaster(fake, cache)
    first = fc.forecast_close(_bars(10))
    assert fc.forecast_close(_bars(10)) == first and len(fake.calls) == 1
    # A fresh forecaster on the same file also hits it: it is on disk.
    fresh = _forecaster(_FakePredictor(factor=2.0), ForecastCache(str(tmp_path / "k" / "cache.db")))
    assert fresh.forecast_close(_bars(10)) == first


def test_cache_key_changes_with_bars_and_settings():
    bars = _bars(8)
    base = ForecastCache.key("BTCUSDT", bars, 4, 3, 1.0, 0.9)
    assert ForecastCache.key("BTCUSDT", bars, 4, 3, 1.0, 0.9) == base
    assert ForecastCache.key("ETHUSDT", bars, 4, 3, 1.0, 0.9) != base
    assert ForecastCache.key("BTCUSDT", bars, 8, 3, 1.0, 0.9) != base
    assert ForecastCache.key("BTCUSDT", bars, 4, 5, 1.0, 0.9) != base
    assert ForecastCache.key("BTCUSDT", _bars(8, start=101.0), 4, 3, 1.0, 0.9) != base


def test_a_new_bar_misses_the_cache(tmp_path):
    fake = _FakePredictor()
    fc = _forecaster(fake, ForecastCache(str(tmp_path / "c.db")))
    fc.forecast_close(_bars(10))
    fc.forecast_close(_bars(11))
    assert len(fake.calls) == 2


# -- availability ----------------------------------------------------------- #
def test_unavailable_reason_explains_what_is_missing(tmp_path):
    assert "MAYA_KRONOS_REPO_PATH" in unavailable_reason("")
    assert "does not look like a Kronos clone" in unavailable_reason(str(tmp_path))
    with pytest.raises(RuntimeError, match="Kronos unavailable"):
        load_predictor("")


# -- Iris ------------------------------------------------------------------- #
def test_iris_carries_the_reading_once_a_15m_bar_closes():
    fake = _FakePredictor()
    proc = SignalProcessor("BTCUSDT", htf_timeframes=("15m",),
                           kronos=_forecaster(fake, lookback=2), kronos_timeframe="15m")
    sig = None
    for i in range(31):                                   # closes two 15m bars
        sig = proc.process(Candle(i * 60_000, 100, 101, 99, 100, 10.0))
        if i < 30:
            assert sig.kronos is None                     # fewer than 2 closed 15m bars
    assert sig.kronos is not None and sig.kronos.direction == "long"
    assert len(fake.calls) == 1                           # once per closed bar, not per minute
    assert proc.process(Candle(31 * 60_000, 100, 101, 99, 100, 10.0)).kronos is sig.kronos


def test_a_failing_forecast_drops_the_reading_not_the_candle():
    class _Boom:
        def signal(self, closed):
            raise RuntimeError("model fell over")
    proc = SignalProcessor("BTCUSDT", htf_timeframes=("15m",), kronos=_Boom())
    sig = None
    for i in range(16):
        sig = proc.process(Candle(i * 60_000, 100, 101, 99, 100, 10.0))
    assert sig is not None and sig.kronos is None


def test_kronos_is_off_by_default_and_iris_has_no_forecaster():
    from maya.journal.store import TradeJournal
    from maya.nodes import TradingNodes
    nodes = TradingNodes(Settings(mode=RunMode.SIM), TradeJournal(":memory:"), now_ts=0)
    assert Settings().kronos_enabled is False and nodes.processor._kronos is None


def test_kronos_timeframe_must_be_one_iris_builds():
    from maya.journal.store import TradeJournal
    from maya.nodes import TradingNodes
    with pytest.raises(ValueError, match="kronos_timeframe"):
        TradingNodes(Settings(mode=RunMode.SIM, kronos_enabled=True, kronos_timeframe="4h"),
                     TradeJournal(":memory:"), now_ts=0)


# -- research --------------------------------------------------------------- #
def test_hypothesis_samples_trade_the_signal_and_hold_the_horizon():
    bars = _bars(8 + KRONOS_STRIDE * 3, step=0.5)         # steadily rising series
    fake = _FakePredictor(factor=1.01)
    samples = kronos_samples(
        {"BTCUSDT": bars},
        forecaster_for=lambda symbol, bar_ms: _forecaster(fake, lookback=8, pred_len=4))
    assert len(samples) == 3                              # one decision every 16 bars
    i = 7
    assert samples[0].timestamp == bars[i].timestamp
    assert samples[0].gross_return == pytest.approx(bars[i + 4].close / bars[i].close - 1)


def test_short_signal_books_the_return_with_the_sign_flipped():
    bars = _bars(8 + KRONOS_STRIDE, step=0.5)
    samples = kronos_samples(
        {"BTCUSDT": bars},
        forecaster_for=lambda s, b: _forecaster(_FakePredictor(factor=0.99), lookback=8))
    assert samples and all(s.gross_return < 0 for s in samples)   # short into a rising series


def test_cli_refuses_the_hypothesis_without_kronos_and_writes_no_row(tmp_path, capsys):
    assert CATALOG["kronos_small_15m"].precheck is not None
    db = str(tmp_path / "j.db")
    assert cli.main(["run", "kronos_small_15m", "--db", db]) == 4
    assert "cannot run kronos_small_15m" in capsys.readouterr().err
    cli.main(["registry", "--db", db])
    assert capsys.readouterr().out.startswith("0 rows")
