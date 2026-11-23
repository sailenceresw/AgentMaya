"""Tests for research diagnostics. The trades and candles here are invented
to make each number checkable by hand."""
from __future__ import annotations

import os

from pytest import approx

from maya.data.models import Candle
from maya.research import diagnose as dg
from maya.research import hypotheses

MIN15 = 15 * 60_000


def _bars(closes: list[float], highs=None, lows=None) -> list[Candle]:
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c
        lo = lows[i] if lows else c
        out.append(Candle(timestamp=i * MIN15, open=c, high=h, low=lo, close=c, volume=1.0))
    return out


def _row(direction="long", entry=100.0, stop=99.0, entry_i=0, exit_i=3, gross=-0.01):
    return {"symbol": "X", "setup_name": "s", "direction": direction, "entry_price": entry,
            "stop_loss": stop, "timestamp_entry": entry_i * MIN15,
            "timestamp_exit": exit_i * MIN15, "gross_return": gross, "exit_reason": "stop_loss"}


def test_long_excursions_in_r_start_after_the_entry_bar():
    # Entry bar's own wick (to 150) happened before the fill and must not count.
    closes = [100, 101, 102, 99, 99, 99]
    highs = [150, 101, 102, 100, 99, 99]
    lows = [50, 100, 101, 99, 99, 99]
    candles = _bars(closes, highs, lows)
    d = dg.diagnose_trade(_row(), candles, [c.timestamp for c in candles], 10.0, 2)
    assert round(d.mfe_r, 6) == 2.0        # +2% on 1% risk
    assert round(d.mae_r, 6) == 1.0        # down to 99
    assert d.forward_bps[1] == approx(100.0) and d.forward_bps[4] == approx(-100.0)
    assert d.net_bps == approx(-110.0) and d.forward_bps[16] is None


def test_short_signs_are_mirrored():
    closes = [100, 99, 98, 101]
    candles = _bars(closes, [100, 99.5, 98.5, 101], [100, 98.8, 97.5, 100])
    d = dg.diagnose_trade(_row("short", stop=101.0, gross=0.01), candles,
                          [c.timestamp for c in candles], 10.0, 2)
    assert round(d.mfe_r, 6) == 2.5        # low 97.5 on a 1% stop
    assert round(d.mae_r, 6) == 1.0        # high 101
    assert d.forward_bps[1] == approx(100.0)   # price fell: good for a short


def _diag(net: float, symbol: str = "A") -> dg.TradeDiag:
    return dg.TradeDiag(symbol, "s", "long", 0, net + 10, net, 50, 0.1, 0.5, 0.5,
                        "take_profit", "asia 00-08", 0.001, True, {h: 1.0 for h in dg.HORIZONS})


def test_a_lead_needs_enough_trades_and_a_large_t():
    strong = [_diag(5.0 + (i % 3)) for i in range(120)]           # n=120, tight, positive
    few = [_diag(50.0 + (i % 2), "B") for i in range(20)]          # big but n=20
    _, cells, leads = dg.group_table(strong + few, lambda d: d.symbol, "symbol")
    assert cells == 2 and len(leads) == 1 and leads[0].startswith("symbol=A")


def test_a_significant_loser_is_not_a_lead():
    """Regression (first baseline_1m_setups merge): cells losing ~10 bps with
    t -10 were listed as leads. Reversing them pays the same costs, so only
    a net-positive cell can lead."""
    losers = [_diag(-10.0 + (i % 3)) for i in range(200)]
    _, cells, leads = dg.group_table(losers, lambda d: d.symbol, "symbol")
    assert cells == 1 and leads == []


def test_report_counts_cells_and_reads_without_trades():
    assert "no trades" in dg.report("h", [], 10.0, 0)
    text = dg.report("h", [_diag(-3.0 + (i % 5)) for i in range(50)], 10.0, 0)
    assert "cells that could lead:" in text and "same entries held" in text


def test_risk_uses_the_original_stop_not_the_moved_one():
    """Regression (first cvd_flip diagnosis): the journal keeps the stop at
    close, after the profit lock moved it to breakeven, so risk came out
    ~0 and the book showed +4.3R on average while losing money."""
    closes = [100, 101, 102, 100.05]
    candles = _bars(closes, [100, 101, 102, 100.1], [100, 100.5, 101, 100.0])
    row = _row(stop=100.05, gross=0.0005, exit_i=3) | {"take_profit": 103.0, "rr_planned": 3.0}
    d = dg.diagnose_trade(row, candles, [c.timestamp for c in candles], 10.0, 2)
    assert d.risk_bps == approx(100.0)       # original stop at 99: (103-100)/3
    assert d.mfe_r == approx(2.0) and d.gross_r == approx(0.05)


def test_exit_reason_is_shown_but_never_a_lead():
    """The exit reason is known only after the trade: it cannot select one."""
    ds = [_diag(-50.0 + (i % 3)) for i in range(150)]
    _, _, leads = dg.group_table(ds, lambda d: d.exit_reason, "exit reason", can_lead=False)
    assert leads == []
    text = dg.report("h", ds, 10.0, 0)
    assert "exit reason=" not in text.split("leads (")[1]


def test_cli_diagnose_never_sees_the_holdout_and_writes_no_row(tmp_path, monkeypatch, capsys):
    from maya.research import __main__ as cli
    seen = {}

    def fake_trades(data, timeframe="1m", **overrides):
        seen["max_ts"] = max(c.timestamp for cs in data.values() for c in cs)
        return [], []

    monkeypatch.setattr(hypotheses, "pipeline_trades", fake_trades)
    monkeypatch.chdir(tmp_path)
    day = 86_400_000
    rows = [[i * MIN15, 100.0, 100.5, 99.5, 100.0, 1.0, 0.5] for i in range(200 * 96)]
    db = str(tmp_path / "j.db")
    rc = cli.main(["diagnose", "cvd_flip", "--symbols", "BTCUSDT", "--db", db],
                  loader=lambda *a, **k: rows)
    assert rc == 0
    newest = rows[-1][0]
    assert seen["max_ts"] < newest - 120 * day          # holdout fenced off
    assert not os.path.exists(db)                       # registry never opened
    # One coin: its own files, so coins can be diagnosed in parallel.
    assert (tmp_path / "data" / "diagnostics" / "cvd_flip_BTCUSDT.txt").exists()
    assert "no trades" in capsys.readouterr().out


def test_per_coin_runs_merge_into_one_report(tmp_path, monkeypatch):
    """Coins can be diagnosed in parallel; the merge must equal one combined run."""
    monkeypatch.chdir(tmp_path)
    out = str(tmp_path / "d")
    os.makedirs(out)
    a, b = [_diag(-5.0 + i % 4, "A") for i in range(30)], [_diag(2.0 + i % 3, "B") for i in range(20)]
    for sym, ds in (("A", a), ("B", b)):
        import csv
        with open(os.path.join(out, f"cvd_flip_{sym}_trades.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(dg.CSV_HEADER)
            for d in ds:
                w.writerow([d.symbol, d.setup, d.direction, d.ts, d.gross_bps, d.net_bps,
                            d.risk_bps, d.gross_r, d.mfe_r, d.mae_r, d.exit_reason, d.session,
                            d.vol, d.with_trend] + [d.forward_bps[h] for h in dg.HORIZONS])
    merged = dg.merge("cvd_flip", ["A", "B"], out_dir=out)
    direct = dg.report("cvd_flip", a + b, 10.0, 0)
    assert merged == direct
    assert (tmp_path / "d" / "cvd_flip.txt").exists()


def test_merge_names_the_missing_coin(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError, match="--symbols ETHUSDT"):
        dg.merge("cvd_flip", ["ETHUSDT"], out_dir=str(tmp_path))


def test_diagnose_rejects_non_pipeline_hypotheses(capsys):
    from maya.research import __main__ as cli
    assert cli.main(["diagnose", "cvd_follow_baseline"]) == 2
