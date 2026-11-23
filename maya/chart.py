"""Draw what Maya sees on a chart, from live or cached candles.

    python -m maya.chart --symbol BTCUSDT --timeframe 15m              # latest 200 bars, live
    python -m maya.chart --symbol ETHUSDT --timeframe 1h --bars 300 --out eth.png
    python -m maya.chart --timeframe 15m --start 2024-08-10 --end 2026-08-09 --bars 250
                                                                       # last bars of a cached range

Live mode reads Binance USD-M public klines (no keys). Only *closed* candles
are drawn: the bar still forming is dropped, because the agent never reads it
either. Drawings are readings, not signals.
"""
from __future__ import annotations

import argparse
import json
import time

from maya.backtest.data_loader import fetch_historical_klines, historical_candles, klines_cache_path
from maya.config import get_settings
from maya.timeframe import timeframe_ms
from maya.viz.chart import chart_payload, render_png


def main(argv: list[str] | None = None, loader=fetch_historical_klines) -> int:
    p = argparse.ArgumentParser(prog="python -m maya.chart", description=__doc__.split("\n")[0])
    p.add_argument("--symbol", default=None, help="defaults to MAYA_SYMBOL")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--bars", type=int, default=200)
    p.add_argument("--start", default=None, help="read a cached range instead of live")
    p.add_argument("--end", default=None)
    p.add_argument("--out", default="chart.png")
    p.add_argument("--json", default=None, help="also write the drawing payload here")
    a = p.parse_args(argv)

    base = get_settings()
    symbol = a.symbol or base.symbol
    settings = base.model_copy(update={"symbol": symbol, "timeframe": a.timeframe})
    step = timeframe_ms(a.timeframe)
    if a.start and a.end:
        from maya.research.__main__ import _ms
        rows = loader(settings, _ms(a.start), _ms(a.end),
                      cache_path=klines_cache_path(symbol, a.timeframe, a.start, a.end))
    else:
        now = int(time.time() * 1000)
        # One extra bar, because the newest is still forming and is dropped.
        rows = loader(settings, now - (a.bars + 2) * step, now)
        rows = [r for r in rows if r[0] + step <= now]
    candles = list(historical_candles(rows))[-a.bars:]
    if not candles:
        print("no candles")
        return 1
    payload = chart_payload(candles, symbol, a.timeframe)
    render_png(payload, a.out)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    d = payload["drawings"] or {}
    print(f"{symbol} {a.timeframe}: {len(candles)} closed bars -> {a.out}")
    for key in ("support_line", "resistance_line"):
        if d.get(key):
            print(f"  {key}: {d[key]['touches']} touches, slope {d[key]['slope_pct_per_bar']}%/bar")
    if d.get("formation"):
        print(f"  formation: {d['formation']}")
    for pat in d.get("patterns", []):
        tgt = f", target {pat['target']:.2f}" if pat.get("target") is not None else ""
        print(f"  pattern: {pat['name']} ({pat['status']}){tgt}")
    for b in d.get("breakouts", []):
        print(f"  breakout {b['direction']}: {b['kind']} at {b['level']:.2f}")
    print(f"  zones drawn: {len(payload['zones'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
