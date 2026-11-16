"""Kronos forecast as one extra input to Iris.

Kronos (github.com/shiyu-coder/Kronos, MIT) is a decoder-only model
pre-trained on candlestick data. Here it does one job: given recent bars it
forecasts the next few, and the forecast becomes a reading on the
:class:`~maya.signals.models.SignalObject` next to CVD, volume profile and
the rest. It is not an agent, it places nothing, and nothing in the strategy
trades on it until its hypothesis (``kronos_small_15m``) passes.

Kronos is not on PyPI. Clone the repo, install its requirements, and point
``MAYA_KRONOS_REPO_PATH`` at the clone. The model and tokenizer download from
Hugging Face (NeoQuasar/Kronos-small, NeoQuasar/Kronos-Tokenizer-base) on
first use. Torch and the model are imported only when a forecast is first
needed, so nothing here costs anything while ``MAYA_KRONOS_ENABLED`` is off.

Forecasts are sampled, so each one averages several samples at a fixed
temperature and top_p, and every result is cached on disk under a key built
from the exact input bars and settings. A backtest that revisits a bar reads
the cache.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass

from loguru import logger

from maya.data.models import Candle

MODEL_ID = "NeoQuasar/Kronos-small"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
# Kronos-small's maximum context. Longer inputs are cut to the newest bars.
MAX_CONTEXT = 512


@dataclass(frozen=True)
class KronosSignal:
    """What the forecast says about the next ``horizon`` bars."""

    direction: str | None       # 'long', 'short' or None (inside the cost band)
    last_close: float
    forecast_close: float
    horizon: int

    @property
    def expected_return(self) -> float:
        return self.forecast_close / self.last_close - 1.0 if self.last_close else 0.0


def signal_rule(last_close: float, forecast_close: float, round_trip: float,
                margin: float) -> str | None:
    """Long only if the forecast beats the last close by more than the round
    trip plus a safety margin; short on the mirror; otherwise no signal.

    ``round_trip`` and ``margin`` are fractions of price (0.001 = 10 bps).
    A forecast inside the band could not pay for its own trade even if it
    were exactly right.
    """
    if last_close <= 0 or forecast_close <= 0:
        return None
    move = forecast_close / last_close - 1.0
    band = round_trip + margin
    if move > band:
        return "long"
    if move < -band:
        return "short"
    return None


class ForecastCache:
    """Forecast closes on disk, keyed by the exact input and settings."""

    def __init__(self, path: str) -> None:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS forecasts (key TEXT PRIMARY KEY, close REAL)")
            self.conn.commit()

    @staticmethod
    def key(symbol: str, candles: list[Candle], pred_len: int, samples: int,
            temperature: float, top_p: float, model_id: str = MODEL_ID) -> str:
        h = hashlib.sha256()
        h.update(f"{model_id}|{symbol}|{pred_len}|{samples}|{temperature}|{top_p}".encode())
        for c in candles:
            h.update(f"|{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}".encode())
        return h.hexdigest()

    def get(self, key: str) -> float | None:
        with self._lock:
            row = self.conn.execute("SELECT close FROM forecasts WHERE key = ?",
                                    (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, close: float) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO forecasts VALUES (?, ?)", (key, close))
            self.conn.commit()


def unavailable_reason(repo_path: str) -> str:
    """Why Kronos cannot run here, or '' if it can. Imports nothing heavy."""
    if not repo_path:
        return "MAYA_KRONOS_REPO_PATH is not set (clone github.com/shiyu-coder/Kronos)"
    if not os.path.isdir(os.path.join(repo_path, "model")):
        return f"{repo_path} does not look like a Kronos clone (no model/ folder)"
    import importlib.util
    for pkg in ("torch", "pandas"):
        if importlib.util.find_spec(pkg) is None:
            return f"{pkg} is not installed (pip install -r {repo_path}/requirements.txt)"
    return ""


def load_predictor(repo_path: str, device: str | None = None):
    """Build the repo's ``KronosPredictor`` for Kronos-small.

    Follows the Kronos README: ``from model import Kronos, KronosTokenizer,
    KronosPredictor``, then ``from_pretrained`` for both. Read from the README,
    not run here; the first real run is the check.
    """
    reason = unavailable_reason(repo_path)
    if reason:
        raise RuntimeError(f"Kronos unavailable: {reason}")
    if repo_path not in sys.path:
        sys.path.insert(0, repo_path)
    from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore[import-not-found]

    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
    model = Kronos.from_pretrained(MODEL_ID)
    kwargs = {"max_context": MAX_CONTEXT}
    if device:
        kwargs["device"] = device
    logger.info("Kronos loaded: {} with {}", MODEL_ID, TOKENIZER_ID)
    return KronosPredictor(model, tokenizer, **kwargs)


class KronosForecaster:
    """Turns recent bars into a :class:`KronosSignal`, with caching.

    ``predictor`` is anything with the repo's ``predict(df, x_timestamp,
    y_timestamp, pred_len, T, top_p, sample_count)`` method. Tests pass a
    fake; in a real run it is loaded on first use.
    """

    def __init__(self, symbol: str, bar_ms: int, round_trip: float, *,
                 repo_path: str = "", lookback: int = 256, pred_len: int = 4,
                 samples: int = 3, temperature: float = 1.0, top_p: float = 0.9,
                 margin_bps: float = 5.0, cache: ForecastCache | None = None,
                 predictor=None, device: str | None = None) -> None:
        if not 2 <= lookback <= MAX_CONTEXT:
            raise ValueError(f"lookback must be 2..{MAX_CONTEXT}")
        if pred_len < 1 or samples < 1:
            raise ValueError("pred_len and samples must be at least 1")
        self.symbol, self.bar_ms, self.round_trip = symbol, bar_ms, round_trip
        self.repo_path, self.lookback, self.pred_len = repo_path, lookback, pred_len
        self.samples, self.temperature, self.top_p = samples, temperature, top_p
        self.margin = margin_bps / 10_000.0
        self.cache, self._predictor, self.device = cache, predictor, device
        self.model_calls = 0

    def forecast_close(self, candles: list[Candle]) -> float | None:
        """Forecast close ``pred_len`` bars after the last one, or None when
        there is not enough history. ``candles`` must be closed bars."""
        if len(candles) < self.lookback:
            return None
        window = candles[-self.lookback:]
        key = ForecastCache.key(self.symbol, window, self.pred_len, self.samples,
                                self.temperature, self.top_p)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        close = self._run_model(window)
        if self.cache is not None:
            self.cache.put(key, close)
        return close

    def _run_model(self, window: list[Candle]) -> float:
        import pandas as pd

        if self._predictor is None:
            self._predictor = load_predictor(self.repo_path, self.device)
        df = pd.DataFrame({
            "open": [c.open for c in window], "high": [c.high for c in window],
            "low": [c.low for c in window], "close": [c.close for c in window],
            "volume": [c.volume for c in window],
        })
        x_ts = pd.Series(pd.to_datetime([c.timestamp for c in window], unit="ms"))
        last = window[-1].timestamp
        y_ts = pd.Series(pd.to_datetime(
            [last + (i + 1) * self.bar_ms for i in range(self.pred_len)], unit="ms"))
        self.model_calls += 1
        pred = self._predictor.predict(
            df=df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=self.pred_len,
            T=self.temperature, top_p=self.top_p, sample_count=self.samples)
        return float(pred["close"].iloc[-1])

    def signal(self, candles: list[Candle]) -> KronosSignal | None:
        close = self.forecast_close(candles)
        if close is None:
            return None
        last = candles[-1].close
        return KronosSignal(signal_rule(last, close, self.round_trip, self.margin),
                            last, close, self.pred_len)
