"""Tests for walk-forward validation."""
import pytest

from maya.backtest.data import demo_dataset
from maya.backtest.walkforward import run_walk_forward
from maya.config import RunMode, Settings


def test_walk_forward_splits_into_folds():
    settings = Settings(mode=RunMode.PAPER, start_equity=1000, symbol="BTCUSDT")
    candles = list(demo_dataset(days=20))
    r = run_walk_forward(settings, candles, folds=4)
    assert r.total_folds == 4
    assert len(r.folds) == 4
    assert r.total_trades > 0
    # Each fold is independent and starts from the same equity.
    for m in r.folds:
        assert m.start_equity == 1000


def test_walk_forward_consistency_flag():
    settings = Settings(mode=RunMode.PAPER, start_equity=1000, symbol="BTCUSDT")
    candles = list(demo_dataset(days=25))
    r = run_walk_forward(settings, candles, folds=5)
    # The repeated-scenario demo is profitable in every fold.
    assert r.profitable_folds == 5
    assert r.consistent is True
    assert r.pass_rate == 1.0


def test_walk_forward_rejects_too_few_candles():
    settings = Settings(mode=RunMode.PAPER, start_equity=1000, symbol="BTCUSDT")
    with pytest.raises(ValueError):
        run_walk_forward(settings, list(demo_dataset(days=1))[:5], folds=5)
