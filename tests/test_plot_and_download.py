"""Tests for the backtest plot and the Binance CSV download helper."""
import csv

import pytest

from maya.backtest import metrics as M
from maya.config import RunMode, Settings


def test_sharpe_clamped_on_degenerate_variance():
    # Perfectly consistent compounding => near-zero variance; must not explode.
    eq = [(i * 86_400_000, 1000 * (1.05 ** i)) for i in range(30)]
    m = M.compute({"trades": 30, "win_rate": 100.0, "profit_factor": 999.99,
                   "total_pnl_usdt": 1.0, "expectancy": 0.03, "total_fees": 0.1},
                  eq, start_equity=1000)
    assert abs(m.sharpe) <= 100.0


def test_save_equity_plot_writes_png(tmp_path):
    pytest.importorskip("matplotlib")
    from maya.backtest.plot import save_equity_plot

    eq = [(i * 86_400_000, 1000 + i * 5) for i in range(40)]
    m = M.compute({"trades": 10, "win_rate": 60.0, "profit_factor": 2.0,
                   "total_pnl_usdt": 200.0, "expectancy": 20.0, "total_fees": 3.0},
                  eq, start_equity=1000)
    out = tmp_path / "equity.png"
    save_equity_plot(eq, m, str(out))
    assert out.exists() and out.stat().st_size > 1000  # a real PNG was written


def test_save_equity_plot_rejects_empty():
    pytest.importorskip("matplotlib")
    from maya.backtest.plot import save_equity_plot
    m = M.compute({"trades": 0}, [], start_equity=1000)
    with pytest.raises(ValueError):
        save_equity_plot([], m, "/tmp/nope.png")


class _FakeEx:
    def __init__(self):
        self._now = 1_700_000_000_000
        self._calls = 0

    def load_markets(self):
        return {}

    def milliseconds(self):
        return self._now

    def iso8601(self, ms):
        return str(ms)

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=1500):
        # Return one short batch then stop (len < limit ends pagination).
        if self._calls:
            return []
        self._calls += 1
        return [[since + i * 60_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i]
                for i in range(3)]


def test_download_writes_csv(tmp_path, monkeypatch):
    from maya.data import download as D
    monkeypatch.setattr(D, "build_exchange", lambda s: _FakeEx())
    out = tmp_path / "klines.csv"
    n = D.download(Settings(mode=RunMode.PAPER, symbol="BTCUSDT"), days=1, out_path=str(out))
    assert n == 3
    rows = list(csv.reader(open(out)))
    assert rows[0] == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(rows) == 4  # header + 3 candles
