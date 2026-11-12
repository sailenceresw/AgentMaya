"""The train/test signal study: no look-ahead, honest verdicts, offline."""
from __future__ import annotations

import numpy as np

from maya.backtest.study import (
    FEATURES,
    WARMUP_BARS,
    LinearModel,
    Score,
    StudyResult,
    build_dataset,
    fit,
    score,
    study,
)
from maya.config import Settings


def _rows(n: int, seed: int = 3) -> list[list]:
    """Invented random-walk klines ``[ts, o, h, l, c, v, taker_buy]``."""
    rng = np.random.default_rng(seed)
    rows, price = [], 100.0
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + rng.normal(0, 0.002)))
        hi, lo = max(o, price) * 1.0005, min(o, price) * 0.9995
        vol = float(rng.uniform(50, 150))
        rows.append([i * 900_000, o, hi, lo, price, vol, vol * float(rng.uniform(0.3, 0.7))])
    return rows


def test_dataset_targets_are_the_next_h_candles_and_nothing_else():
    rows = _rows(WARMUP_BARS + 60)
    X, y = build_dataset(Settings(timeframe="15m"), rows, horizon=4)
    assert X.shape == (len(rows) - 4 - WARMUP_BARS, len(FEATURES))
    closes = [r[4] for r in rows]
    i = WARMUP_BARS                       # first kept bar
    assert abs(y[0] - (closes[i + 4] / closes[i] - 1) * 1e4) < 1e-9
    assert abs(y[-1] - (closes[-1] / closes[-5] - 1) * 1e4) < 1e-9


def test_features_do_not_read_the_future():
    """Changing every candle after bar k must leave bar k's features alone."""
    rows = _rows(WARMUP_BARS + 80)
    k = WARMUP_BARS + 30
    tampered = [list(r) for r in rows]
    for r in tampered[k + 1:]:
        r[1:5] = [x * 3 for x in r[1:5]]
        r[5] *= 9
    s = Settings(timeframe="15m")
    X1, _ = build_dataset(s, rows, horizon=4)
    X2, _ = build_dataset(s, tampered, horizon=4)
    assert np.allclose(X1[: k - WARMUP_BARS + 1], X2[: k - WARMUP_BARS + 1])


def _planted(n: int, strength: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, len(FEATURES)))
    return X, strength * X[:, 0] + rng.normal(0, 10.0, size=n)


def test_a_planted_edge_is_found_and_survives_the_test_window():
    r = study(_planted(20_000, 30.0, 1), _planted(5_000, 30.0, 2), horizon=1,
              round_trip_bps=7.0)
    assert r.test.ic > 0.9 and r.test.t > 2
    assert r.test.decile_edge_bps > 7.0
    assert r.verdict.startswith("candidate")
    assert r.features[0].holds and abs(r.features[0].weight_bps - 30.0) < 1.0


def test_noise_is_called_noise_even_though_it_fits_the_train_window():
    """Ten features always fit *something* in sample. The verdict must come
    from the window the model never saw."""
    r = study(_planted(400, 0.0, 1), _planted(4_000, 0.0, 2), horizon=1, round_trip_bps=7.0)
    assert r.train.ic > 0                 # in-sample fit is never negative
    assert abs(r.test.t) < 2
    assert r.verdict.startswith("no predictive signal")


def test_an_edge_smaller_than_the_round_trip_is_not_a_candidate():
    """Real but too small to pay the venue: the README's measured case."""
    r = study(_planted(200_000, 1.0, 1), _planted(200_000, 1.0, 2), horizon=1,
              round_trip_bps=26.0)
    assert r.test.t > 2
    assert r.test.decile_edge_bps < 26.0
    assert r.verdict.startswith("predicts, but not enough to trade")


def test_overlapping_targets_do_not_inflate_the_t_stat():
    X, y = _planted(8_000, 2.0, 5)
    m1, m4 = fit(X, y, horizon=1), fit(X, y, horizon=4)
    assert score(m4, X, y).t < score(m1, X, y).t
    assert score(m4, X, y).ic == score(m1, X, y).ic


def test_empty_test_window_is_reported_not_scored():
    m = LinearModel(FEATURES, [0.0] * 10, [1.0] * 10, [0.0] * 10, 0.0, 4)
    empty = score(m, np.empty((0, 10)), np.empty(0))
    assert empty == Score(0, 0.0, 0.0, 0.0, 0.0)
    assert StudyResult(m, empty, empty, 7.0).verdict == "no test data"
