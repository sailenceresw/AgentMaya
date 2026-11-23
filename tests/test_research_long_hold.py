"""The research-only hold override, the funding charge and the trend test.

The 48h hold exists for one pre-declared research test. These pin that it can
only be set through run_backtest, never through a setting a live runner reads.
"""
from __future__ import annotations

import pytest

from maya.config import RiskLimits, RunMode, Settings
from maya.data.models import Candle
from maya.execution.engine import ExecutionEngine
from maya.execution.models import Position
from maya.research import __main__ as cli
from maya.research import hypotheses
from maya.research.hypotheses import CATALOG, PIPELINE_CONFIGS, trend_samples

H = 3_600_000
DAY = 86_400_000


def _bar(ts: int, close: float) -> Candle:
    return Candle(timestamp=ts, open=close, high=close + 0.1, low=close - 0.1,
                  close=close, volume=10, taker_buy_volume=5)


def _engine_with_position() -> ExecutionEngine:
    eng = ExecutionEngine(Settings(mode=RunMode.SIM))
    eng.position = Position("BTCUSDT", "long", "x", 100, 90, 130, quantity=0.1,
                            notional=10, leverage=5, opened_ts=0, confluence_score=8,
                            rr_planned=3.0)
    return eng


def test_hold_limit_defaults_to_the_hard_limit():
    assert ExecutionEngine(Settings(mode=RunMode.SIM)).max_hold_hours == \
        RiskLimits.MAX_POSITION_DURATION_HOURS == 4
    # No setting can carry it into a live or testnet runner.
    assert not any("hold" in f for f in Settings.model_fields)


def test_default_engine_force_closes_at_4h():
    eng = _engine_with_position()
    res = eng.manage(_bar(4 * H, 100.0))
    assert res is not None and res.reason == "max_duration"


def test_raised_hold_keeps_the_trade_open_past_4h():
    eng = _engine_with_position()
    eng.max_hold_hours = 48
    assert eng.manage(_bar(4 * H, 100.0)) is None
    assert eng.manage(_bar(47 * H, 100.0)) is None
    res = eng.manage(_bar(48 * H, 100.0))
    assert res is not None and res.reason == "max_duration"


def test_run_backtest_refuses_a_nonsense_hold():
    from maya.backtest.runner import run_backtest
    with pytest.raises(ValueError):
        run_backtest(Settings(mode=RunMode.SIM), iter([]), max_hold_hours=0)


def test_pipeline_override_keys_never_reach_settings(monkeypatch):
    seen = {}

    def fake_backtest(settings, candles, journal_path, progress_every, max_hold_hours):
        seen["hold"] = max_hold_hours
        seen["fields"] = settings.model_dump()
        import sqlite3

        from maya.journal.store import TradeJournal
        j = TradeJournal(db_path=journal_path)
        j.conn.execute(
            "INSERT INTO trades (id, timestamp_entry, timestamp_exit, symbol, position_size,"
            " entry_price, pnl_usdt, fees_usdt) VALUES ('t', 0, ?, 'BTCUSDT', 1, 100, 1, 0)",
            (9 * H,))   # 9h held: two started 8h funding periods
        j.conn.commit()
        j.conn.close()
        del sqlite3

        class M:
            maker = None
        return M()

    monkeypatch.setattr("maya.backtest.runner.run_backtest", fake_backtest)
    rows, _ = hypotheses.pipeline_trades({"BTCUSDT": []}, "1h", max_hold_hours=48,
                                         funding_bps_per_8h=1.0)
    assert seen["hold"] == 48
    assert "max_hold_hours" not in seen["fields"] and "funding_bps_per_8h" not in seen["fields"]
    # 1% gross on notional, minus 2 funding periods of 1 bps.
    assert rows[0]["gross_return"] == pytest.approx(0.01 - 0.0002)


def test_48h_hypothesis_is_declared_with_its_coins():
    tf, over = PIPELINE_CONFIGS["baseline_1h_48h_hold"]
    assert tf == "1h" and over == {"max_hold_hours": 48, "funding_bps_per_8h": 1.0}
    assert len(CATALOG["baseline_1h_48h_hold"].symbols.split(",")) == 15
    # The original 1h test keeps its shipped 4h limit.
    assert PIPELINE_CONFIGS["baseline_1h_setups"] == ("1h", {})


def test_cli_uses_declared_coins_unless_told_otherwise():
    hyp = CATALOG["trend_30d_hold_7d"]
    assert len(cli._symbols(None, hyp)) == 15
    assert cli._symbols("BTCUSDT", hyp) == ["BTCUSDT"]
    assert cli._symbols(None, CATALOG["cvd_follow_baseline"]) == \
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_older_trend_test_is_the_same_rules_on_a_window_before_the_first():
    old, new = CATALOG["trend_30d_hold_7d_2020_2024"], CATALOG["trend_30d_hold_7d"]
    assert old.generate is new.generate and old.symbols == new.symbols
    assert old.timeframe == new.timeframe == "1d"
    assert (old.start, old.end) == ("2020-01-01", "2024-08-09")
    assert old.end < cli.DEFAULT_START          # no overlap with the window already seen

    class A:
        start = end = None
    assert cli._window(A, old) == ("2020-01-01", "2024-08-09")
    assert cli._window(A, new) == (cli.DEFAULT_START, cli.DEFAULT_END)
    A.start = "2021-01-01"
    assert cli._window(A, old) == ("2021-01-01", "2024-08-09")


def test_trend_follows_the_30_day_direction_without_overlap():
    # Invented series: rises steadily, so every decision is long and wins.
    up = [_bar(i * DAY, 100.0 + i) for i in range(60)]
    s = trend_samples({"X": up})
    funding = 7 * 3 * 1.0 / 10_000
    # Decisions at day 30, 37, 44, 51 (52 + 7 > 59 leaves no full hold).
    assert [x.timestamp // DAY for x in s] == [30, 37, 44, 51]
    assert s[0].gross_return == pytest.approx((137 / 130 - 1) - funding)
    down = [_bar(i * DAY, 200.0 - i) for i in range(60)]
    assert all(x.gross_return > 0 for x in trend_samples({"X": down}))   # short wins


def test_trend_never_reads_past_the_hold():
    # 50 bars: decisions at days 30 and 37; the second hold ends at day 44.
    # A bar after that must not change any sample.
    bars = [_bar(i * DAY, 100.0 + (i % 7)) for i in range(50)]
    a = trend_samples({"X": bars})
    assert [x.timestamp // DAY for x in a] == [30, 37]
    bars2 = bars[:-1] + [_bar(49 * DAY, 999.0)]
    assert trend_samples({"X": bars2}) == a
