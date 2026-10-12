"""Offline data import and backtest charts."""
from __future__ import annotations

import zipfile

import pytest

from maya.backtest.data_loader import klines_cache_path
from maya.backtest.metrics import compute_metrics
from maya.data import download

# Two rows in data.binance.vision's format (with its header), one in µs.
DUMP = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore\n"
    "1735689600000,93500.1,93800,93400,93700.5,1200.5,1735693199999,1.1e8,50000,640.2,6e7,0\n"
    "1735693200000000,93700.5,94000,93600,93900,1100,1735696799999999,1e8,48000,500.1,4.7e7,0\n"
)


def test_dump_rows_keep_taker_buy_and_normalise_microseconds():
    rows = download.parse_dump_rows(DUMP)
    assert rows[0] == [1735689600000, 93500.1, 93800.0, 93400.0, 93700.5, 1200.5, 640.2]
    assert rows[1][0] == 1735693200000           # µs -> ms
    assert rows[1][6] == 500.1


def test_import_merges_zip_and_csv_filters_range_and_dedupes(tmp_path):
    z = tmp_path / "BTCUSDT-1h-2025-01.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("BTCUSDT-1h-2025-01.csv", DUMP)
    c = tmp_path / "again.csv"
    c.write_text(DUMP)                              # overlapping file
    rows = download.import_dumps([str(z), str(c)], 1735689600000, 1735693200000)
    assert [r[0] for r in rows] == [1735689600000, 1735693200000]


def test_cli_import_writes_where_the_backtester_looks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "dump.csv"
    f.write_text(DUMP)
    download.main(["--import-dumps", str(f), "--symbols", "BTCUSDT", "--timeframes", "1h",
                   "--start", "2025-01-01", "--end", "2025-01-02"])
    out = tmp_path / klines_cache_path("BTCUSDT", "1h", "2025-01-01", "2025-01-02")
    assert out.exists() and "taker_buy_volume" in out.read_text().splitlines()[0]


def test_equity_plot_is_written(tmp_path):
    pytest.importorskip("matplotlib")
    from maya.backtest.plot import save_equity_plot

    day = 86_400_000
    m = compute_metrics({0: 1000.0, day: 1010.0, 2 * day: 990.0, 3 * day: 1030.0}, [], 1000.0)
    assert len(m.equity_curve) == 4
    out = tmp_path / "eq.png"
    save_equity_plot(m, str(out), title="BTCUSDT 1h")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_empty_curve_is_refused():
    from maya.backtest.plot import save_equity_plot

    with pytest.raises(ValueError):
        save_equity_plot(compute_metrics({}, [], 1000.0), "x.png")
