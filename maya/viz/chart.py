"""Chart payload and renderer.

``chart_payload`` turns closed candles into one JSON document: the candles,
Atlas's support/resistance zones, everything ``chart_patterns.read_chart``
draws, and the open position's entry/stop/target. The dashboard draws that
document on a canvas and ``render_png`` draws the same document with
matplotlib, so the phone and a saved image can never disagree.

Drawing only. Nothing here feeds a trading decision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from maya.agents.atlas import find_levels
from maya.data.models import Candle
from maya.signals.chart_patterns import read_chart

# Zones weaker than this are left off the chart: Atlas keeps every cluster,
# and drawing thirty faint bands hides the three that matter.
MIN_ZONE_STRENGTH = 0.35
MAX_ZONES_PER_SIDE = 4


def chart_payload(candles: list[Candle], symbol: str = "", timeframe: str = "",
                  position: Any | None = None) -> dict[str, Any]:
    """Everything needed to draw the chart, JSON-ready."""
    candles = list(candles)
    payload: dict[str, Any] = {
        "symbol": symbol, "timeframe": timeframe,
        "candles": [[c.timestamp, c.open, c.high, c.low, c.close, c.volume] for c in candles],
        "zones": [], "drawings": None, "position": None,
    }
    if not candles:
        return payload
    levels = find_levels(candles)
    price = candles[-1].close

    def pick(zones):
        # One touch is a coincidence, not a level (Atlas's own docstring).
        strong = [z for z in zones if z.strength >= MIN_ZONE_STRENGTH and z.touches >= 2]
        strong.sort(key=lambda z: abs(z.center - price))
        return strong[:MAX_ZONES_PER_SIDE]

    shown = pick(levels.support) + pick(levels.resistance)
    payload["zones"] = [{"kind": z.kind, "lower": z.lower, "upper": z.upper,
                         "center": z.center, "touches": z.touches,
                         "strength": z.strength} for z in shown]
    payload["drawings"] = read_chart(candles, zones=shown).to_dict(candles)
    if position is not None:
        payload["position"] = {
            "direction": position.direction, "setup": getattr(position, "setup_name", ""),
            "entry": position.entry_price, "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "opened_ts": getattr(position, "opened_ts", None),
        }
    return payload


def render_png(payload: dict[str, Any], path: str) -> None:
    """Draw ``payload`` to a PNG. Needs matplotlib (optional dependency)."""
    rows = payload.get("candles") or []
    if not rows:
        raise ValueError("no candles to draw")
    try:
        import matplotlib
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError("charts need matplotlib: pip install matplotlib") from exc
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    ts = [r[0] for r in rows]
    step = (ts[1] - ts[0]) if len(ts) > 1 else 60_000
    x_of = {t: i for i, t in enumerate(ts)}

    def x(t: int) -> float:
        # Timestamps map to bar positions so gaps (weekends on other markets,
        # missing bars) do not stretch the chart.
        if t in x_of:
            return float(x_of[t])
        return (t - ts[0]) / step

    fig, ax = plt.subplots(figsize=(13, 6.5))
    fig.patch.set_facecolor("#0b0f14")
    ax.set_facecolor("#0b0f14")
    up, down, dim = "#3fb950", "#f85149", "#8b9bb0"
    n = len(rows)

    for z in payload.get("zones", []):
        color = up if z["kind"] == "support" else down
        ax.add_patch(Rectangle((0, z["lower"]), n, z["upper"] - z["lower"],
                               color=color, alpha=0.08 + 0.17 * z["strength"], lw=0))
        ax.text(n + 0.5, z["center"], f"{z['kind'][0].upper()} x{z['touches']}",
                color=color, fontsize=7, va="center")

    for i, (_, o, h, lo, c, _) in enumerate(rows):
        color = up if c >= o else down
        ax.plot([i, i], [lo, h], color=color, lw=0.7)
        ax.add_patch(Rectangle((i - 0.35, min(o, c)), 0.7, max(abs(c - o), 1e-9),
                               color=color, lw=0))

    d = payload.get("drawings") or {}
    for p in d.get("pivots", []):
        ax.plot(x(p[0]), p[1], marker="v" if p[2] == "high" else "^",
                color=dim, ms=3.5, alpha=0.7)
    for key, color in (("support_line", up), ("resistance_line", down)):
        line = d.get(key)
        if line:
            (t1, p1), (t2, p2) = line["points"]
            ax.plot([x(t1), x(t2)], [p1, p2], color=color, lw=1.4,
                    label=f"{key.replace('_', ' ')} ({line['touches']} touches)")
    if d.get("formation"):
        ax.text(0.01, 0.97, d["formation"].replace("_", " "), transform=ax.transAxes,
                color="#58a6ff", fontsize=10, va="top", weight="bold")
    # The price axis follows the candles (and an open position), not the
    # drawings: a Fibonacci extension or a far line must not squash the bars.
    lows = [r[3] for r in rows] + ([payload["position"][k] for k in ("stop_loss", "take_profit")]
                                   if payload.get("position") else [])
    highs = [r[2] for r in rows] + ([payload["position"][k] for k in ("stop_loss", "take_profit")]
                                    if payload.get("position") else [])
    ylo, yhi = min(lows), max(highs)
    ypad = (yhi - ylo) * 0.06 or 1.0
    ylo, yhi = ylo - ypad, yhi + ypad
    fib = d.get("fib")
    if fib:
        for name, level in fib["levels"].items():
            if not ylo <= level <= yhi:
                continue
            ax.axhline(level, color="#d29922", lw=0.6, ls=":", alpha=0.7)
            ax.text(-0.5, level, f"fib {name}", color="#d29922", fontsize=7, va="bottom",
                    bbox={"facecolor": "#0b0f14", "edgecolor": "none", "pad": 0.5, "alpha": 0.8})
    for pat in d.get("patterns", []):
        color = up if pat["direction"] == "bullish" else down
        xs = [x(t) for t, _ in pat["points"]]
        ax.plot(xs, [v for _, v in pat["points"]], color=color, lw=1, ls="--", alpha=0.8)
        if pat.get("neckline"):
            (t1, v1), (t2, v2) = pat["neckline"]
            ax.plot([x(t1), x(t2)], [v1, v2], color=color, lw=1, ls="-.")
        ax.text(xs[-1], pat["points"][-1][1], f" {pat['name'].replace('_', ' ')} ({pat['status']})",
                color=color, fontsize=8)
        if pat.get("target") is not None:
            ax.axhline(pat["target"], color=color, lw=0.6, ls=(0, (1, 4)))
    for b in d.get("breakouts", []):
        ax.annotate(f"breakout {b['direction']}: {b['kind'].replace('_', ' ')}",
                    xy=(n - 1, b["level"]), xytext=(n - 12, b["level"]),
                    color="#58a6ff", fontsize=8,
                    arrowprops={"arrowstyle": "->", "color": "#58a6ff"})
    pos = payload.get("position")
    if pos:
        for key, color, label in (("entry", "#58a6ff", "entry"), ("stop_loss", down, "SL"),
                                  ("take_profit", up, "TP")):
            ax.axhline(pos[key], color=color, lw=1.1)
            ax.text(n + 0.5, pos[key], f"{label} {pos[key]:.2f}", color=color,
                    fontsize=8, va="center")

    ticks = list(range(0, n, max(n // 8, 1)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([datetime.fromtimestamp(ts[i] / 1000, tz=timezone.utc)
                        .strftime("%m-%d %H:%M") for i in ticks], color=dim, fontsize=8)
    ax.tick_params(colors=dim, labelsize=8)
    for s in ax.spines.values():
        s.set_color("#223041")
    ax.set_xlim(-1, n + 6)
    ax.set_ylim(ylo, yhi)
    ax.grid(alpha=0.08)
    ax.set_title(f"{payload.get('symbol', '')} {payload.get('timeframe', '')} - what Maya sees "
                 "(drawings are readings, not signals)", color="#e6edf3", fontsize=11)
    if ax.get_legend_handles_labels()[0]:
        ax.legend(loc="lower left", fontsize=8, facecolor="#141b24", labelcolor="#e6edf3",
                  edgecolor="#223041")
    fig.tight_layout()
    fig.savefig(path, dpi=120, facecolor=fig.get_facecolor())
    plt.close(fig)


def live_candles(nodes: Any, timeframe: str) -> list[Candle]:
    """Closed candles the running agent holds for ``timeframe``.

    The base timeframe comes from the signal processor's window, higher ones
    from its aggregators. The API thread reads these while the trading thread
    appends, and copying a deque that is being appended to can raise; retry a
    few times rather than failing the request.
    """
    proc = getattr(nodes, "processor", None)
    if proc is None:
        raise ValueError("no chart in scanner mode: it holds no single symbol's candles")
    source = None
    if timeframe in ("", "base", nodes.settings.timeframe):
        source = proc.candles
    else:
        for agg in proc.aggregators:
            if agg.label == timeframe:
                source = agg.closed
                break
    if source is None:
        raise ValueError(f"no candles held for timeframe {timeframe!r}")
    for _ in range(5):
        try:
            return list(source)
        except RuntimeError:  # deque mutated during iteration
            continue
    return []


def scanner_chart(agent: Any, timeframe: str = "base", bars: int = 150,
                  coin: str | None = None, fetcher_for=None) -> dict[str, Any]:
    """A chart for the multi-coin scanner, which keeps no candles in memory.

    Fetches the coin's recent closed candles on request (the same Binance
    source the scan reads). The coin is ``coin`` if given, else the open
    trade's, else the best candidate of the last scan, else the first coin
    scanned. Runs on the caller's thread (Telegram's), never the scan loop.
    """
    import re

    settings = agent.settings
    tf = settings.timeframe if timeframe in ("", "base") else timeframe
    # Checked before any download: a typo ("7x") must not become a venue request.
    if not re.fullmatch(r"\d+[smhdw]", tf):
        raise ValueError(f"unknown timeframe {tf!r}; use e.g. 1m, 5m, 15m, 1h, 4h")
    scan = list(getattr(agent, "last_scan", None) or [])
    pos = agent.execution.position
    by_coin = {c.market.coin.upper(): c.market.binance_symbol for c in scan}
    if coin:
        symbol = by_coin.get(coin.upper().removesuffix("USDT"), coin.upper().removesuffix("USDT")
                             + "USDT")
    elif pos is not None:
        symbol = pos.symbol
    elif scan:
        best = next((c for c in scan if c.intent is not None), scan[0])
        symbol = best.market.binance_symbol
    else:
        symbol = settings.symbol
    if tf == settings.timeframe and fetcher_for is None:
        fetch = agent.scanner.fetch_candles
    else:
        if fetcher_for is None:
            from maya.scanner import binance_candle_fetcher
            fetcher_for = lambda t: binance_candle_fetcher(  # noqa: E731
                settings.model_copy(update={"timeframe": t}))
        fetch = fetcher_for(tf)
    candles = fetch(symbol, bars)
    if not candles:
        raise ValueError(f"no candles for {symbol} {tf}")
    shown = pos if pos is not None and pos.symbol == symbol else None
    payload = chart_payload(candles, symbol, tf, position=shown)
    payload["available"] = [settings.timeframe, "5m", "15m", "1h", "4h"]
    return payload


def agent_chart(nodes: Any, timeframe: str = "base", bars: int = 150,
                coin: str | None = None) -> dict[str, Any]:
    """Chart for whichever agent is running: the scanner or the single-coin one."""
    if getattr(nodes, "scanner", None) is not None:
        return scanner_chart(nodes, timeframe, bars, coin)
    if coin and coin.upper().removesuffix("USDT") != nodes.settings.symbol.removesuffix("USDT"):
        raise ValueError(f"this agent trades {nodes.settings.symbol} only")
    return live_chart(nodes, timeframe, bars)


def live_chart(nodes: Any, timeframe: str = "base", bars: int = 150) -> dict[str, Any]:
    """The /api/chart document for a running agent."""
    tf = nodes.settings.timeframe if timeframe in ("", "base") else timeframe
    candles = live_candles(nodes, timeframe)[-bars:]
    payload = chart_payload(candles, nodes.settings.symbol, tf,
                            position=nodes.execution.position)
    payload["available"] = [nodes.settings.timeframe] + [
        a.label for a in nodes.processor.aggregators]
    return payload
