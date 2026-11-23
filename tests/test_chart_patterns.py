"""Tests for chart drawings (trendlines, formations, patterns, Fibonacci,
breakouts), the chart payload/renderer, the /api/chart route and the
chart_breakout_15m hypothesis.

Every price path here is invented, built point by point to contain exactly
the shape under test. They prove the detectors find what they claim to find
and never read a bar that has not closed. They say nothing about whether any
of it predicts price; that is what chart_breakout_15m is for.
"""
from __future__ import annotations

import json
import random
import urllib.error
import urllib.request

import pytest

from maya.api.server import DASHBOARD_HTML, MonitorServer, json_safe
from maya.config import RunMode, Settings
from maya.data.models import Candle
from maya.research.hypotheses import CATALOG, CHART_HOLD, chart_breakout_samples
from maya.signals import chart_patterns as cp
from maya.viz.chart import chart_payload, live_chart, render_png

MIN = 60_000
WICK = 0.1
TOKEN = "chart-token"


def _path(points: list[tuple[int, float]]) -> list[float]:
    """Piecewise-linear closes through (bar, price) waypoints."""
    closes: list[float] = []
    for (i0, p0), (i1, p1) in zip(points, points[1:], strict=False):
        for i in range(i0, i1):
            closes.append(p0 + (p1 - p0) * (i - i0) / (i1 - i0))
    closes.append(points[-1][1])
    return closes


def _candles(closes: list[float]) -> list[Candle]:
    out: list[Candle] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(timestamp=i * 15 * MIN, open=o, high=max(o, c) + WICK,
                          low=min(o, c) - WICK, close=c, volume=1.0 + (i % 3)))
        prev = c
    return out


def _zigzag(lows: list[float], highs: list[float], leg: int = 6) -> list[Candle]:
    """Alternate low, high, low, high ... ``leg`` bars apart."""
    pts, bar = [], 0
    for lo, hi in zip(lows, highs, strict=True):
        pts += [(bar, lo), (bar + leg, hi)]
        bar += 2 * leg
    pts.append((bar, lows[-1] + (highs[-1] - lows[-1]) / 2))
    return _candles(_path(pts))


# -- pivots: confirmed only ------------------------------------------------- #
def test_newest_bars_can_never_be_pivots():
    rng = random.Random(3)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] + rng.uniform(-1, 1))
    candles = _candles(closes)
    piv = cp.pivots(candles)
    assert piv and max(p.index for p in piv) <= len(candles) - 1 - cp.SWING_LOOKAROUND


def test_a_confirmed_pivot_does_not_change_when_more_bars_arrive():
    """No lookahead: once a pivot is confirmed, future bars cannot move it."""
    rng = random.Random(11)
    closes = [100.0]
    for _ in range(250):
        closes.append(closes[-1] + rng.uniform(-1, 1))
    candles = _candles(closes)
    k = 180
    early = {(p.index, p.kind) for p in cp.pivots(candles[:k])}
    later = {(p.index, p.kind) for p in cp.pivots(candles) if p.index < k - cp.SWING_LOOKAROUND}
    assert early == later


def test_too_little_history_reads_nothing():
    assert cp.read_chart(_candles([100, 101, 102])).pivots == []


# -- trendlines and formations ---------------------------------------------- #
def test_rising_parallel_zigzag_is_an_ascending_channel():
    candles = _zigzag([100, 102, 104, 106, 108], [105, 107, 109, 111, 113])
    r = cp.read_chart(candles)
    assert r.support_line is not None and r.support_line.touches >= 3
    assert r.support_line.slope > 0 and r.resistance_line.slope > 0
    assert r.formation == "ascending_channel"


def test_flat_highs_and_rising_lows_are_an_ascending_triangle():
    # Gentle enough that the half-leg down after the last top stays above the
    # rising support line; a steeper one would (correctly) break it.
    candles = _zigzag([100, 101.5, 103, 104.5], [110, 110, 110, 110])
    r = cp.read_chart(candles)
    assert r.formation == "ascending_triangle"


def test_falling_highs_and_rising_lows_are_a_symmetrical_triangle():
    candles = _zigzag([100, 102, 104, 106], [120, 117, 114, 111])
    assert cp.read_chart(candles).formation == "symmetrical_triangle"


def test_a_line_broken_by_an_earlier_close_is_rejected():
    """Respect means no close through the line between its anchors and now."""
    candles = _zigzag([100, 102, 104, 106], [105, 107, 109, 111])
    # Knock one mid-series close far below the rising lows.
    mid = 20
    c = candles[mid]
    candles[mid] = Candle(c.timestamp, c.open, c.high, 90.0, 90.5, c.volume)
    sup = cp.find_trendline(candles, cp.pivots(candles), "support", cp.median_range(candles))
    if sup is not None:  # any surviving line must start after the break
        assert sup.i1 > mid


def test_lines_need_spaced_anchors_and_must_stay_near_price():
    """Regression (live BTC 15m, 2026-11-17): two highs a few bars apart made a
    steep 'resistance' line that ran off the top of the chart. Anchors must be
    MIN_ANCHOR_GAP bars apart and the line must end near the traded range."""
    candles = _zigzag([100, 102, 104, 106, 108], [105, 107, 109, 111, 113])
    unit = cp.median_range(candles)
    for kind in ("support", "resistance"):
        line = cp.find_trendline(candles, cp.pivots(candles), kind, unit)
        assert line is not None and line.i2 - line.i1 >= cp.MIN_ANCHOR_GAP
        lo = min(c.low for c in candles) - cp.LINE_IN_PLAY_RANGES * unit
        hi = max(c.high for c in candles) + cp.LINE_IN_PLAY_RANGES * unit
        assert lo <= line.price_at(len(candles) - 1) <= hi
    # Two pivots closer than the minimum gap can never anchor a line alone.
    close = [cp.Pivot(10, 0, 100.0, "high"), cp.Pivot(14, 0, 110.0, "high")]
    assert cp.find_trendline(candles, close, "resistance", unit) is None


def test_close_through_rising_support_is_a_downside_breakout():
    candles = _zigzag([100, 102, 104, 106, 108], [105, 107, 109, 111, 113])
    last = candles[-1]
    candles.append(Candle(last.timestamp + 15 * MIN, last.close, last.close + WICK,
                          95.0, 95.2, 1.0))
    r = cp.read_chart(candles)
    assert any(b.kind == "support_line" and b.direction == "down" for b in r.breakouts)


# -- classical patterns ------------------------------------------------------ #
def test_double_top_confirms_on_the_neckline_break():
    closes = _path([(0, 100), (10, 110), (18, 104), (26, 110), (34, 103)])
    candles = _candles(closes)
    at_break = cp.read_chart(candles[:34])          # bar 33 closes through the neck
    pat = next(p for p in at_break.patterns if p.name == "double_top")
    assert pat.status == "confirmed" and pat.direction == "bearish"
    assert pat.target == pytest.approx(pat.neckline[0][1] - (110 + WICK - pat.neckline[0][1]))
    assert any(b.kind == "double_top" and b.direction == "down" for b in at_break.breakouts)
    # One bar later the break is old news: still a pattern, no longer a breakout.
    after = cp.read_chart(candles)
    assert not any(b.kind == "double_top" for b in after.breakouts)


def test_double_bottom_is_the_mirror():
    closes = _path([(0, 110), (10, 100), (18, 106), (26, 100), (34, 107)])
    r = cp.read_chart(_candles(closes))
    pat = next(p for p in r.patterns if p.name == "double_bottom")
    assert pat.status == "confirmed" and pat.direction == "bullish"


def test_unequal_tops_are_not_a_double_top():
    closes = _path([(0, 100), (10, 110), (18, 104), (26, 116), (34, 103)])
    assert not any(p.name == "double_top" for p in cp.read_chart(_candles(closes)).patterns)


def test_head_and_shoulders():
    closes = _path([(0, 100), (8, 108), (14, 103), (22, 113), (30, 103), (38, 108), (46, 101)])
    r = cp.read_chart(_candles(closes))
    pat = next(p for p in r.patterns if p.name == "head_and_shoulders")
    assert pat.status == "confirmed" and pat.direction == "bearish"
    assert pat.target < pat.neckline[-1][1]


def test_inverse_head_and_shoulders():
    closes = _path([(0, 120), (8, 112), (14, 117), (22, 107), (30, 117), (38, 112), (46, 119)])
    pat = next(p for p in cp.read_chart(_candles(closes)).patterns
               if p.name == "inverse_head_and_shoulders")
    assert pat.status == "confirmed" and pat.direction == "bullish"


def test_bull_flag_breakout():
    base = [100.0 + (0.05 if i % 2 else 0.0) for i in range(20)]
    pole = _path([(0, 100), (8, 112)])[1:]
    flag = _path([(0, 112), (8, 110.5)])[1:]
    candles = _candles(base + pole + flag + [113.0])
    pat = next(p for p in cp.read_chart(candles).patterns if p.name == "bull_flag")
    assert pat.status == "confirmed" and pat.target > 113


def test_fibonacci_levels_on_an_up_leg():
    closes = _path([(0, 105), (5, 100), (25, 120), (35, 112)])
    fib = cp.read_chart(_candles(closes)).fib
    assert fib is not None and fib.direction == "up"
    lo, hi = fib.start[1], fib.end[1]
    assert fib.levels["0.618"] == pytest.approx(hi - 0.618 * (hi - lo))
    assert fib.levels["1.618"] == pytest.approx(lo + 1.618 * (hi - lo))


# -- payload, renderer ------------------------------------------------------- #
class _Pos:
    direction, setup_name = "long", "liquidity_sweep_choch"
    entry_price, stop_loss, take_profit, opened_ts = 110.0, 108.0, 116.0, 0


def test_payload_is_strict_json_and_carries_every_layer():
    candles = _zigzag([100, 102, 104, 106, 108], [105, 107, 109, 111, 113])
    payload = chart_payload(candles, "BTCUSDT", "15m", position=_Pos())
    json.dumps(json_safe(payload), allow_nan=False)
    assert len(payload["candles"]) == len(candles)
    assert payload["drawings"]["support_line"]["points"][0][0] == candles[
        cp.read_chart(candles).support_line.i1].timestamp
    assert payload["position"]["stop_loss"] == 108.0
    assert set(payload) >= {"zones", "drawings", "position", "symbol", "timeframe"}


def test_empty_payload_draws_nothing():
    assert chart_payload([], "BTCUSDT", "15m")["drawings"] is None


def test_render_png_writes_an_image(tmp_path):
    pytest.importorskip("matplotlib")
    closes = _path([(0, 100), (8, 108), (14, 103), (22, 113), (30, 103), (38, 108), (46, 101)])
    out = tmp_path / "c.png"
    render_png(chart_payload(_candles(closes), "BTCUSDT", "15m", position=_Pos()), str(out))
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_cli_draws_from_a_loader(tmp_path, capsys):
    pytest.importorskip("matplotlib")
    from maya import chart as cli
    closes = _path([(0, 100), (10, 110), (18, 104), (26, 110), (34, 103)])
    rows = [[c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.volume / 2]
            for c in _candles(closes)]
    out = tmp_path / "x.png"
    rc = cli.main(["--symbol", "BTCUSDT", "--timeframe", "15m", "--start", "2024-01-01",
                   "--end", "2024-01-02", "--out", str(out)],
                  loader=lambda *a, **k: rows)
    assert rc == 0 and out.exists()
    assert "double_top" in capsys.readouterr().out


# -- live agent and API ------------------------------------------------------ #
def _scenario_nodes(tmp_path):
    from maya.data.ingestion import build_feed
    from maya.graph import build_graph
    from maya.journal.store import TradeJournal
    from maya.nodes import TradingNodes
    settings = Settings(mode=RunMode.SIM, scenario=True, max_candles=0)
    nodes = TradingNodes(settings, TradeJournal(db_path=str(tmp_path / "j.db")))
    graph = build_graph(nodes)
    for candle in build_feed(settings).candles():
        graph.invoke({"candle": candle})
    return nodes


def test_live_chart_reads_the_agents_own_candles(tmp_path):
    nodes = _scenario_nodes(tmp_path)
    payload = live_chart(nodes, "base", 50)
    assert 0 < len(payload["candles"]) <= 50
    assert payload["candles"][-1][0] == nodes.processor.candles[-1].timestamp
    assert payload["available"][0] == nodes.settings.timeframe
    with pytest.raises(ValueError):
        live_chart(nodes, "7m", 50)


def test_drawing_does_not_touch_trading_state(tmp_path):
    """Reading the chart must leave the agent exactly as it was."""
    nodes = _scenario_nodes(tmp_path)
    before = (len(nodes.processor.candles), nodes.risk.account.equity,
              nodes.execution.position)
    live_chart(nodes, "base", 100)
    assert (len(nodes.processor.candles), nodes.risk.account.equity,
            nodes.execution.position) == before


def _server(chart_fn=None):
    s = Settings(api_enabled=True, api_token=TOKEN, api_host="127.0.0.1", api_port=0)
    srv = MonitorServer(s, lambda: {}, lambda n: [], lambda r: None, chart_fn=chart_fn)
    srv.start()
    return srv


def _get(srv, path, token=TOKEN):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_chart_route_clamps_bars_and_needs_the_token():
    seen = []

    def fn(tf, bars):
        seen.append((tf, bars))
        if tf == "bad":
            raise ValueError("no candles held for timeframe 'bad'")
        return {"candles": []}

    srv = _server(fn)
    try:
        assert _get(srv, "/api/chart?tf=15m&bars=99999")[0] == 200
        assert seen[-1] == ("15m", 300)
        assert _get(srv, "/api/chart?bars=1")[0] == 200 and seen[-1] == ("base", 20)
        assert _get(srv, "/api/chart?tf=bad")[0] == 400
        assert _get(srv, "/api/chart", token=None)[0] == 401
    finally:
        srv.stop()


def test_chart_route_is_absent_without_a_chart_function():
    srv = _server()
    try:
        assert _get(srv, "/api/chart")[0] == 404
    finally:
        srv.stop()


def test_dashboard_asks_for_the_chart_but_survives_without_it():
    assert "/api/chart?tf=" in DASHBOARD_HTML and ".catch(() => null)" in DASHBOARD_HTML


# -- hypothesis -------------------------------------------------------------- #
def test_chart_breakout_hypothesis_is_declared_and_never_overlaps():
    hyp = CATALOG["chart_breakout_15m"]
    assert hyp.timeframe == "15m" and not hyp.blocked
    rng = random.Random(5)
    closes = [100.0]
    for _ in range(900):
        closes.append(closes[-1] * (1 + rng.uniform(-0.004, 0.004)))
    samples = chart_breakout_samples({"X": _candles(closes)})
    assert samples, "a 900-bar random walk should break some line"
    gaps = [b.timestamp - a.timestamp for a, b in zip(samples, samples[1:], strict=False)]
    assert min(gaps) >= CHART_HOLD * 15 * MIN
