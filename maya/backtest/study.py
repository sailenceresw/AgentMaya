"""Do the signals predict anything? Fit on one period, score on a later one.

The sweep answers "did the strategy make money" from a few hundred trades,
which is too few to tell a small edge from luck and says nothing about *why*.
This asks the question underneath it, on every closed candle instead of only
the ones that became trades:

    Given everything the signal layer knows when a candle closes, which way
    does price go over the next H candles — and is the move bigger than a
    round trip costs?

It runs the live :class:`~maya.signals.processor.SignalProcessor` and
:class:`~maya.strategy.engine.StrategyEngine` context unmodified, turns each
:class:`~maya.signals.models.SignalObject` into signed, unit-free features,
fits one linear model on the **train** window and scores it on a **test**
window it never saw. Nothing is tuned on the test window: the features, the
horizon and the ridge penalty are fixed before it is read.

    python -m maya.backtest.study --symbols BTCUSDT,ETHUSDT,SOLUSDT \\
        --timeframe 15m --start 2024-08-10 --end 2026-08-09 \\
        --test-start 2026-08-10 --test-end 2026-10-31

Data comes from the same ``data/`` cache as the sweep (``maya.data.download``).
Without ``--test-start`` the last 30% of the window is held out instead.

Reading the output:

* **IC** is the correlation between the model's forecast and what happened.
  0 is no information; 0.02-0.05 is a real but small edge.
* **t** is the IC's t-statistic, counting one bar in every H as independent
  because consecutive targets overlap. Under 2 is indistinguishable from
  noise.
* **Top-decile edge** is the average move, in the forecast's direction, on
  the 10% of bars the model was most confident about each way — the best
  case a threshold strategy built on it could hope for *before* fees. It is
  compared with the venue's worst-case round trip from :mod:`maya.fees`.

This is a measuring instrument, not a strategy. The fitted model is not
wired into the graph and ``--save`` only writes it to disk for inspection.
A "candidate" verdict means "worth confirming on later data", never "trade
this" — see the README's measurement notes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from loguru import logger

from maya.backtest.data_loader import _load_csv, historical_candles, klines_cache_path
from maya.config import Settings
from maya.fees import FeeModel
from maya.signals.models import SignalObject
from maya.signals.processor import SignalProcessor
from maya.strategy.engine import StrategyEngine
from maya.strategy.setups import SetupContext

# Signed so that a positive value is the reading the *setups* treat as
# bullish. A negative fitted weight therefore means the market did the
# opposite of what the strategy assumes.
FEATURES = (
    "taker_imbalance",    # (taker buys - taker sells) / volume on this candle
    "bar_return_atr",     # close-to-close move, in ATRs
    "close_in_range",     # where the close sits in the candle: -0.5 low .. +0.5 high
    "volume_surge_dir",   # relative volume, signed by the candle's direction
    "value_area_pos",     # price vs POC, in value-area widths (above = +)
    "absorption",         # +strength bullish absorption, -strength bearish
    "cvd_divergence",     # +1 bullish, -1 bearish
    "pattern_bias",       # +1 bullish candlestick read, -1 bearish
    "htf_trend",          # mean of higher-timeframe trends: up +1, down -1
    "liquidity_sweep",    # +1 swept the prior low and closed back, -1 the high
)

# Fixed before any test window is read. Standardised features with a penalty
# this small is ordinary least squares with a guard against collinearity.
RIDGE_LAMBDA = 1.0
# Bars the processor needs before its rolling windows mean anything.
WARMUP_BARS = 200
_SIGN = {"bullish": 1.0, "bearish": -1.0, "up": 1.0, "down": -1.0}


def _clip(x: float, lim: float) -> float:
    return max(-lim, min(lim, x))


def feature_row(sig: SignalObject, ctx: SetupContext, prev_close: float | None) -> list[float]:
    """One candle's features. Uses only what is known at that candle's close."""
    price = sig.price
    rng = sig.high - sig.low
    atr = sig.atr if sig.atr and sig.atr > 0 else None
    bar_ret = (price - prev_close) if prev_close else 0.0
    direction = 1.0 if price > sig.open else -1.0 if price < sig.open else 0.0
    rel_vol = (sig.volume / sig.avg_volume - 1.0) if sig.avg_volume > 0 else 0.0
    vp = sig.volume_profile
    va_width = (vp.vah - vp.val) if vp else 0.0
    htf = [_SIGN.get(c.trend, 0.0) for c in sig.htf.values()]
    return [
        _clip(sig.cvd_1m / sig.volume, 1.0) if sig.volume > 0 else 0.0,
        _clip(bar_ret / atr, 5.0) if atr else 0.0,
        ((price - sig.low) / rng - 0.5) if rng > 0 else 0.0,
        _clip(rel_vol, 5.0) * direction,
        _clip((price - vp.poc) / va_width, 3.0) if va_width > 0 else 0.0,
        _SIGN.get(sig.absorption.side or "", 0.0) * sig.absorption.strength
        if sig.absorption.detected else 0.0,
        _SIGN.get(sig.cvd_divergence or "", 0.0),
        _SIGN.get(sig.pattern_bias, 0.0),
        sum(htf) / len(htf) if htf else 0.0,
        (1.0 if ctx.swept_low else 0.0) - (1.0 if ctx.swept_high else 0.0),
    ]


def build_dataset(settings: Settings, rows: list[list], horizon: int
                  ) -> tuple[np.ndarray, np.ndarray]:
    """``(X, y)`` for one symbol: features at each close, and the return over
    the next ``horizon`` candles in basis points.

    The last ``horizon`` candles have no future yet and are dropped, so a
    target never reaches outside the window it was built from.
    """
    processor = SignalProcessor(settings.symbol, htf_timeframes=settings.htf_list)
    strategy = StrategyEngine(settings)
    feats: list[list[float]] = []
    closes: list[float] = []
    prev_close: float | None = None
    for candle in historical_candles(rows):
        sig = processor.process(candle)
        ctx = strategy.observe(sig)
        feats.append(feature_row(sig, ctx, prev_close))
        closes.append(candle.close)
        prev_close = candle.close
    n = len(closes) - horizon
    if n <= WARMUP_BARS:
        return np.empty((0, len(FEATURES))), np.empty(0)
    c = np.asarray(closes)
    y = (c[horizon:] / c[:-horizon] - 1.0) * 1e4
    return np.asarray(feats)[WARMUP_BARS:n], y[WARMUP_BARS:n]


@dataclass
class LinearModel:
    """Standardise, then a dot product. Small enough to read by eye."""

    features: tuple[str, ...]
    mean: list[float]
    std: list[float]
    weights: list[float]          # bps of forecast per 1 standard deviation
    intercept: float
    horizon: int
    trained_on: str = ""

    def predict(self, X: np.ndarray) -> np.ndarray:
        z = (X - np.asarray(self.mean)) / np.asarray(self.std)
        return z @ np.asarray(self.weights) + self.intercept


def fit(X: np.ndarray, y: np.ndarray, horizon: int, trained_on: str = "") -> LinearModel:
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0            # a feature that never moved gets weight 0
    z = (X - mean) / std
    intercept = float(y.mean())
    k = z.shape[1]
    w = np.linalg.solve(z.T @ z + RIDGE_LAMBDA * np.eye(k), z.T @ (y - intercept))
    return LinearModel(FEATURES, mean.tolist(), std.tolist(), w.tolist(), intercept,
                       horizon, trained_on)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _t_stat(ic: float, n: int) -> float:
    if n < 3 or abs(ic) >= 1:
        return 0.0
    return ic * math.sqrt((n - 2) / (1 - ic * ic))


@dataclass
class Score:
    n: int                        # bars scored
    ic: float
    t: float                      # of the IC, on n / horizon independent bars
    decile_edge_bps: float        # mean move in the forecast's direction, top+bottom 10%
    decile_hit_rate: float        # share of those bars that moved the forecast's way


def score(model: LinearModel, X: np.ndarray, y: np.ndarray) -> Score:
    if len(y) == 0:
        return Score(0, 0.0, 0.0, 0.0, 0.0)
    pred = model.predict(X)
    ic = _corr(pred, y)
    # Consecutive targets share horizon-1 of their candles, so the bars are
    # not independent observations: count one in every `horizon`.
    independent = len(y) // max(model.horizon, 1)
    lo, hi = np.quantile(pred, [0.1, 0.9])
    signed = np.concatenate([y[pred >= hi], -y[pred <= lo]])
    return Score(
        n=len(y), ic=round(ic, 4), t=round(_t_stat(ic, independent), 2),
        decile_edge_bps=round(float(signed.mean()), 2) if len(signed) else 0.0,
        decile_hit_rate=round(float((signed > 0).mean()), 3) if len(signed) else 0.0,
    )


@dataclass
class FeatureRead:
    name: str
    weight_bps: float
    ic_train: float
    ic_test: float

    @property
    def holds(self) -> bool:
        """Same sign in both windows — the minimum for calling it real."""
        return self.ic_train * self.ic_test > 0


@dataclass
class StudyResult:
    model: LinearModel
    train: Score
    test: Score
    round_trip_bps: float
    features: list[FeatureRead] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        t, edge = self.test.t, self.test.decile_edge_bps
        if self.test.n == 0:
            return "no test data"
        if t < 2.0:
            return ("no predictive signal out of sample "
                    f"(t={t:+.2f}; under 2 is noise)")
        if edge <= self.round_trip_bps:
            return (f"predicts, but not enough to trade: best-case {edge:.1f} bps a trade "
                    f"against a {self.round_trip_bps:.1f} bps round trip")
        return ("candidate — the edge clears fees on this test window; confirm on later "
                "data and through the full backtest before trusting it")


def study(model_data: tuple[np.ndarray, np.ndarray], test_data: tuple[np.ndarray, np.ndarray],
          horizon: int, round_trip_bps: float, trained_on: str = "") -> StudyResult:
    Xtr, ytr = model_data
    Xte, yte = test_data
    model = fit(Xtr, ytr, horizon, trained_on)
    reads = [FeatureRead(name, round(model.weights[i], 3),
                         round(_corr(Xtr[:, i], ytr), 4),
                         round(_corr(Xte[:, i], yte), 4) if len(yte) else 0.0)
             for i, name in enumerate(FEATURES)]
    return StudyResult(model, score(model, Xtr, ytr), score(model, Xte, yte),
                       round_trip_bps, reads)


def format_result(r: StudyResult, title: str = "") -> str:
    out = [title] if title else []
    out.append(f"{'':<8}{'bars':>9}{'IC':>9}{'t':>8}{'top-decile edge':>18}{'hit rate':>10}")
    for label, s in (("train", r.train), ("TEST", r.test)):
        out.append(f"{label:<8}{s.n:>9}{s.ic:>+9.4f}{s.t:>+8.2f}"
                   f"{s.decile_edge_bps:>+14.2f} bps{s.decile_hit_rate:>10.1%}")
    out.append(f"round trip to beat: {r.round_trip_bps:.1f} bps (worst case, this venue)")
    out.append("")
    out.append(f"{'feature':<20}{'weight bps/sd':>14}{'IC train':>10}{'IC test':>10}  holds?")
    for f in sorted(r.features, key=lambda f: -abs(f.weight_bps)):
        out.append(f"{f.name:<20}{f.weight_bps:>+14.3f}{f.ic_train:>+10.4f}{f.ic_test:>+10.4f}"
                   f"  {'yes' if f.holds else 'NO — sign flipped'}")
    out.append("")
    out.append(f"verdict: {r.verdict}")
    return "\n".join(out)


def _load(args: tuple[Settings, str, str, int]) -> tuple[np.ndarray, np.ndarray]:
    settings, start, end, horizon = args
    path = klines_cache_path(settings.symbol, settings.timeframe, start, end)
    if not Path(path).exists():
        raise FileNotFoundError(
            f"{path} not found — fetch it first with `python -m maya.data.download`")
    return build_dataset(settings, _load_csv(path), horizon)


def _stack(parts: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    return np.vstack([p[0] for p in parts]), np.concatenate([p[1] for p in parts])


def main(argv: list[str] | None = None) -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    p = argparse.ArgumentParser(prog="maya.backtest.study",
                                description=__doc__.split("\n")[0])
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--start", default="2025-01-01", help="train window start")
    p.add_argument("--end", default="2025-12-31", help="train window end")
    p.add_argument("--test-start", default=None,
                   help="separate test window (else the last 30%% of the train window is held out)")
    p.add_argument("--test-end", default=None)
    p.add_argument("--horizon", type=int, default=4, help="candles ahead to forecast")
    p.add_argument("--save", default=None, help="write the fitted model here as JSON")
    a = p.parse_args(argv)
    if bool(a.test_start) != bool(a.test_end):
        p.error("--test-start and --test-end go together")

    from maya.config import get_settings

    base = get_settings()
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    per_symbol = [base.model_copy(update={"symbol": s, "timeframe": a.timeframe,
                                          "llm_enabled": False}) for s in symbols]
    jobs = [(s, a.start, a.end, a.horizon) for s in per_symbol]
    if a.test_start:
        jobs += [(s, a.test_start, a.test_end, a.horizon) for s in per_symbol]
    logger.info("Building features for {} window(s) — a few minutes on 15m", len(jobs))
    with ProcessPoolExecutor() as pool:
        parts = list(pool.map(_load, jobs))

    first = parts[:len(symbols)]
    if a.test_start:
        train, test = first, parts[len(symbols):]
        split = f"train {a.start}..{a.end}, test {a.test_start}..{a.test_end}"
    else:
        # Chronological, per symbol: the test bars are all later than the
        # train bars, with `horizon` bars dropped between so no train target
        # overlaps the test window.
        train, test = [], []
        for X, y in first:
            cut = int(len(y) * 0.7)
            train.append((X[:max(cut - a.horizon, 0)], y[:max(cut - a.horizon, 0)]))
            test.append((X[cut:], y[cut:]))
        split = f"{a.start}..{a.end}, first 70% train / last 30% test"

    fees = FeeModel.for_settings(base)
    order_type = base.perps_order_type if base.venue.value == "hyperliquid" else "limit"
    result = study(_stack(train), _stack(test), a.horizon,
                   fees.worst_round_trip(order_type) * 1e4, trained_on=split)
    print()
    print(format_result(
        result, f"{','.join(symbols)} {a.timeframe}, {a.horizon} candles ahead — {split} "
                f"(venue={base.venue.value})"))
    if a.save:
        Path(a.save).parent.mkdir(parents=True, exist_ok=True)
        Path(a.save).write_text(json.dumps(
            {"model": asdict(result.model), "train": asdict(result.train),
             "test": asdict(result.test), "round_trip_bps": result.round_trip_bps,
             "verdict": result.verdict}, indent=2))
        logger.info("Model written to {} (not used by the agent)", a.save)


if __name__ == "__main__":
    main()
