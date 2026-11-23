"""Chart drawings a discretionary trader makes, computed mechanically.

Trendlines, channels, triangles and wedges, double tops and bottoms, head and
shoulders, flags, Fibonacci retracements, and breakouts of any of them.

**These are readings, not signals.** Nothing in the strategy, the desk or the
risk layer reads them. They exist so the dashboard and ``python -m
maya.chart`` can draw what the chart looks like, and so the research harness
can measure them (``chart_breakout_15m``) before anything trades on them. The
24-month study found that the classical readings already in Maya (zones,
candlestick patterns, structure) do not predict; these get the same test
before they get any say.

Rules that keep them honest:

- Everything is built from *confirmed* pivots (``lookaround`` bars on both
  sides, the same rule as ``technicals.swing_points``), so the newest bars can
  never be a pivot and nothing here can see a turn before it has happened.
- A trendline must be unbroken by any *close* between its first anchor and
  the bar before the latest one. The latest close is left free so that it can
  be the bar that breaks it, which is what a breakout is.
- Tolerances scale with the median bar range of the window, not with a fixed
  percentage, so the same code reads a 1m and a 1d chart.
- Every parameter is a module constant, fixed before any measurement. Change
  one and it is a new hypothesis, not a fix.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, Iterable

import numpy as np

from maya.data.models import Candle
from maya.signals.technicals import SWING_LOOKAROUND

# Pivots considered per side when searching for lines and patterns. More finds
# older lines nobody is watching any more; fewer misses the obvious one.
MAX_PIVOTS = 8
# A trendline's two anchors must be at least this many bars apart. Two highs a
# few bars apart define a slope by accident, and extended across the chart
# that slope points anywhere (seen on live BTC 15m: a "resistance" line that
# left the top of the chart).
MIN_ANCHOR_GAP = 10
# A line is only in play if, at the latest bar, it sits within this many
# median bar ranges of the window's traded range.
LINE_IN_PLAY_RANGES = 2.0
# A close may poke through a line by this many median bar ranges and still
# count as respecting it (wicks always may).
LINE_TOLERANCE_RANGES = 0.25
# A pivot within this many median bar ranges of a line counts as a touch.
TOUCH_TOLERANCE_RANGES = 0.5
# Slopes are measured as a fraction of price per bar. Below this a line is
# "flat" (0.005% per bar is 0.5% over 100 bars).
FLAT_SLOPE = 0.00005
# Two lines are parallel (a channel) when their slopes differ by less than
# this share of the steeper one.
PARALLEL_SHARE = 0.3
# Lines converge (triangle/wedge) when the gap between them has shrunk to at
# most this share of the gap where the pattern began.
CONVERGE_SHARE = 0.75
# Double tops/bottoms and shoulders: "equal" means within this many median
# bar ranges, and the dip between them must be at least MIN_DEPTH ranges.
EQUAL_RANGES = 1.0
MIN_DEPTH_RANGES = 3.0
# Flags: a pole of POLE_BARS that moves at least POLE_MIN_RANGES median
# ranges, then FLAG_MIN..FLAG_MAX bars of consolidation no taller than
# FLAG_MAX_SHARE of the pole.
POLE_BARS = 8
POLE_MIN_RANGES = 4.0
FLAG_MIN, FLAG_MAX = 5, 15
FLAG_MAX_SHARE = 0.5
FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
FIB_EXTENSIONS = (1.272, 1.618)


@dataclass(frozen=True)
class Pivot:
    index: int
    ts: int
    price: float
    kind: str  # 'high' | 'low'


@dataclass
class Trendline:
    kind: str  # 'support' | 'resistance'
    i1: int
    p1: float
    i2: int
    p2: float
    ts1: int
    ts2: int
    touches: int

    @property
    def slope(self) -> float:
        return (self.p2 - self.p1) / (self.i2 - self.i1)

    def price_at(self, i: float) -> float:
        return self.p1 + self.slope * (i - self.i1)

    def slope_norm(self) -> float:
        """Slope as a fraction of price per bar."""
        return self.slope / self.p2 if self.p2 else 0.0


@dataclass
class ChartPattern:
    name: str
    direction: str            # 'bullish' | 'bearish'
    status: str               # 'forming' | 'confirmed'
    points: list[tuple[int, float]]          # (index, price) of the defining pivots
    neckline: list[tuple[int, float]] | None = None
    target: float | None = None              # measured-move objective


@dataclass
class Fibonacci:
    direction: str            # 'up' leg (low->high) or 'down' leg (high->low)
    start: tuple[int, float]
    end: tuple[int, float]
    levels: dict[str, float]


@dataclass
class Breakout:
    kind: str                 # what broke: 'support_line', 'resistance_line', 'zone', a pattern name
    direction: str            # 'up' | 'down'
    level: float


@dataclass
class ChartReading:
    pivots: list[Pivot] = field(default_factory=list)
    support_line: Trendline | None = None
    resistance_line: Trendline | None = None
    formation: str | None = None   # channel / triangle / wedge between the two lines
    patterns: list[ChartPattern] = field(default_factory=list)
    fib: Fibonacci | None = None
    breakouts: list[Breakout] = field(default_factory=list)
    unit: float = 0.0              # median bar range the tolerances were scaled by

    def to_dict(self, candles: list[Candle]) -> dict[str, Any]:
        """Timestamped, JSON-ready form for the API and the renderer."""
        n = len(candles)

        def ts(i: int) -> int:
            # A line extended to the right edge can be asked for the latest
            # bar; anything else maps to its own candle.
            return candles[min(max(i, 0), n - 1)].timestamp

        def line(t: Trendline | None) -> dict[str, Any] | None:
            if t is None:
                return None
            last = n - 1
            return {"kind": t.kind, "touches": t.touches,
                    "slope_pct_per_bar": round(t.slope_norm() * 100, 5),
                    "points": [[t.ts1, t.p1], [ts(last), t.price_at(last)]]}

        return {
            "pivots": [[p.ts, p.price, p.kind] for p in self.pivots],
            "support_line": line(self.support_line),
            "resistance_line": line(self.resistance_line),
            "formation": self.formation,
            "patterns": [{
                "name": p.name, "direction": p.direction, "status": p.status,
                "points": [[ts(i), v] for i, v in p.points],
                "neckline": [[ts(i), v] for i, v in p.neckline] if p.neckline else None,
                "target": p.target,
            } for p in self.patterns],
            "fib": None if self.fib is None else {
                "direction": self.fib.direction,
                "start": [ts(self.fib.start[0]), self.fib.start[1]],
                "end": [ts(self.fib.end[0]), self.fib.end[1]],
                "levels": self.fib.levels,
            },
            "breakouts": [asdict(b) for b in self.breakouts],
        }


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def pivots(candles: list[Candle], lookaround: int = SWING_LOOKAROUND) -> list[Pivot]:
    """Confirmed swing highs and lows, in time order, with their bar index.

    Same rule as ``technicals.swing_points`` so the chart and the strategy
    agree on what a swing is.
    """
    out: list[Pivot] = []
    for i in range(lookaround, len(candles) - lookaround):
        window = candles[i - lookaround: i + lookaround + 1]
        c = candles[i]
        if c.high == max(w.high for w in window) and c.high > candles[i - 1].high:
            out.append(Pivot(i, c.timestamp, c.high, "high"))
        if c.low == min(w.low for w in window) and c.low < candles[i - 1].low:
            out.append(Pivot(i, c.timestamp, c.low, "low"))
    return out


def median_range(candles: list[Candle]) -> float:
    rs = [c.high - c.low for c in candles if c.high > c.low]
    return median(rs) if rs else 0.0


def find_trendline(candles: list[Candle], piv: list[Pivot], kind: str,
                   unit: float) -> Trendline | None:
    """Best unbroken line through two of the recent pivots on one side.

    Support joins lows, resistance joins highs. Best = most touches, then the
    most recent second anchor, then the longest span.
    """
    side = "low" if kind == "support" else "high"
    pts = [p for p in piv if p.kind == side][-MAX_PIVOTS:]
    n = len(candles)
    if len(pts) < 2 or n < 3:
        return None
    closes = np.array([c.close for c in candles])
    idx = np.arange(n)
    tol = LINE_TOLERANCE_RANGES * unit
    touch_tol = TOUCH_TOLERANCE_RANGES * unit
    floor = min(c.low for c in candles) - LINE_IN_PLAY_RANGES * unit
    ceiling = max(c.high for c in candles) + LINE_IN_PLAY_RANGES * unit
    best: tuple[tuple[int, int, int], Trendline] | None = None
    for a in range(len(pts)):
        for b in range(a + 1, len(pts)):
            pa, pb = pts[a], pts[b]
            if pb.index - pa.index < MIN_ANCHOR_GAP:
                continue
            slope = (pb.price - pa.price) / (pb.index - pa.index)
            line = pa.price + slope * (idx - pa.index)
            if not floor <= line[-1] <= ceiling:
                continue
            # Respect check from the first anchor to the bar before the latest.
            seg = slice(pa.index, n - 1)
            if kind == "support":
                if np.any(closes[seg] < line[seg] - tol):
                    continue
            elif np.any(closes[seg] > line[seg] + tol):
                continue
            touches = sum(1 for p in pts if abs(p.price - line[p.index]) <= touch_tol)
            key = (touches, pb.index, pb.index - pa.index)
            if best is None or key > best[0]:
                best = (key, Trendline(kind, pa.index, pa.price, pb.index, pb.price,
                                       pa.ts, pb.ts, touches))
    return best[1] if best else None


def classify_formation(sup: Trendline | None, res: Trendline | None, n: int) -> str | None:
    """Name what the two lines make together: a channel, triangle or wedge."""
    if sup is None or res is None:
        return None
    ss, sr = sup.slope_norm(), res.slope_norm()
    start = max(sup.i1, res.i1)
    gap_start = res.price_at(start) - sup.price_at(start)
    gap_now = res.price_at(n - 1) - sup.price_at(n - 1)
    if gap_start <= 0 or gap_now <= 0:
        return None  # lines crossed: no formation between them
    flat_s, flat_r = abs(ss) < FLAT_SLOPE, abs(sr) < FLAT_SLOPE
    converging = gap_now <= CONVERGE_SHARE * gap_start
    if converging:
        if flat_r and ss > FLAT_SLOPE:
            return "ascending_triangle"
        if flat_s and sr < -FLAT_SLOPE:
            return "descending_triangle"
        if sr < -FLAT_SLOPE and ss > FLAT_SLOPE:
            return "symmetrical_triangle"
        if ss > FLAT_SLOPE and sr > FLAT_SLOPE:
            return "rising_wedge"
        if ss < -FLAT_SLOPE and sr < -FLAT_SLOPE:
            return "falling_wedge"
        return None
    steeper = max(abs(ss), abs(sr))
    if (flat_s and flat_r) or abs(ss - sr) <= PARALLEL_SHARE * steeper:
        if flat_s and flat_r:
            return "horizontal_channel"
        return "ascending_channel" if ss > 0 else "descending_channel"
    return None


def _between(piv: list[Pivot], kind: str, lo: int, hi: int) -> list[Pivot]:
    return [p for p in piv if p.kind == kind and lo < p.index < hi]


def _extreme_between(candles: list[Candle], lo: int, hi: int, kind: str) -> tuple[int, float]:
    """Lowest low (kind='low') or highest high between two bars, exclusive."""
    rng = range(lo + 1, hi)
    if not rng:
        return lo, candles[lo].low if kind == "low" else candles[lo].high
    if kind == "low":
        i = min(rng, key=lambda j: candles[j].low)
        return i, candles[i].low
    i = max(rng, key=lambda j: candles[j].high)
    return i, candles[i].high


def find_double(candles: list[Candle], piv: list[Pivot], unit: float,
                top: bool) -> ChartPattern | None:
    """Double top (two equal highs with a dip between) or its mirror."""
    kind = "high" if top else "low"
    pts = [p for p in piv if p.kind == kind]
    if len(pts) < 2 or unit <= 0:
        return None
    a, b = pts[-2], pts[-1]
    if abs(a.price - b.price) > EQUAL_RANGES * unit:
        return None
    ni, neck = _extreme_between(candles, a.index, b.index, "low" if top else "high")
    extreme = max(a.price, b.price) if top else min(a.price, b.price)
    depth = (extreme - neck) if top else (neck - extreme)
    if depth < MIN_DEPTH_RANGES * unit:
        return None
    after = candles[b.index + 1:]
    # Invalidated if price has since closed beyond the tops/bottoms.
    if any((c.close > extreme + EQUAL_RANGES * unit) if top else
           (c.close < extreme - EQUAL_RANGES * unit) for c in after):
        return None
    last = candles[-1].close
    confirmed = last < neck if top else last > neck
    return ChartPattern(
        name="double_top" if top else "double_bottom",
        direction="bearish" if top else "bullish",
        status="confirmed" if confirmed else "forming",
        points=[(a.index, a.price), (ni, neck), (b.index, b.price)],
        neckline=[(a.index, neck), (len(candles) - 1, neck)],
        target=(neck - depth) if top else (neck + depth),
    )


def find_head_shoulders(candles: list[Candle], piv: list[Pivot], unit: float,
                        inverse: bool) -> ChartPattern | None:
    """Head and shoulders (three highs, middle highest, shoulders level) or inverse."""
    kind = "low" if inverse else "high"
    pts = [p for p in piv if p.kind == kind]
    if len(pts) < 3 or unit <= 0:
        return None
    ls, hd, rs = pts[-3], pts[-2], pts[-1]
    if inverse:
        if not (hd.price < min(ls.price, rs.price) - EQUAL_RANGES * unit):
            return None
    elif not (hd.price > max(ls.price, rs.price) + EQUAL_RANGES * unit):
        return None
    if abs(ls.price - rs.price) > 2 * EQUAL_RANGES * unit:
        return None
    other = "high" if inverse else "low"
    i1, n1 = _extreme_between(candles, ls.index, hd.index, other)
    i2, n2 = _extreme_between(candles, hd.index, rs.index, other)
    if i2 == i1:
        return None
    slope = (n2 - n1) / (i2 - i1)
    n = len(candles)
    neck_now = n1 + slope * (n - 1 - i1)
    height = (n1 + slope * (hd.index - i1)) - hd.price if inverse else \
        hd.price - (n1 + slope * (hd.index - i1))
    if height < MIN_DEPTH_RANGES * unit:
        return None
    after = candles[rs.index + 1:]
    if any((c.close < hd.price) if inverse else (c.close > hd.price) for c in after):
        return None  # head taken out: the pattern failed
    last = candles[-1].close
    confirmed = last > neck_now if inverse else last < neck_now
    return ChartPattern(
        name="inverse_head_and_shoulders" if inverse else "head_and_shoulders",
        direction="bullish" if inverse else "bearish",
        status="confirmed" if confirmed else "forming",
        points=[(ls.index, ls.price), (hd.index, hd.price), (rs.index, rs.price)],
        neckline=[(i1, n1), (n - 1, neck_now)],
        target=(neck_now + height) if inverse else (neck_now - height),
    )


def find_flag(candles: list[Candle], unit: float) -> ChartPattern | None:
    """A sharp pole followed by a small counter-drift, ending at the bar before
    the latest; the latest bar either breaks out of it or not yet."""
    n = len(candles)
    if unit <= 0 or n < POLE_BARS + FLAG_MIN + 1:
        return None
    best: ChartPattern | None = None
    best_score = 0.0
    for flen in range(FLAG_MIN, FLAG_MAX + 1):
        f0 = n - 1 - flen          # first flag bar
        p0 = f0 - POLE_BARS        # first pole bar
        if p0 < 0:
            break
        pole = candles[p0:f0]
        flag = candles[f0:n - 1]
        move = pole[-1].close - pole[0].open
        if abs(move) < POLE_MIN_RANGES * unit:
            continue
        hi, lo = max(c.high for c in flag), min(c.low for c in flag)
        if hi - lo > FLAG_MAX_SHARE * abs(move):
            continue
        drift = flag[-1].close - flag[0].close
        bull = move > 0
        # A flag drifts against the pole or sideways, never with it.
        if (bull and drift > 0.25 * (hi - lo)) or (not bull and drift < -0.25 * (hi - lo)):
            continue
        # Several pole/flag splits can qualify; the cleanest is the one whose
        # pole is tallest relative to the flag's height.
        score = abs(move) / max(hi - lo, 1e-12)
        if score <= best_score:
            continue
        best_score = score
        last = candles[-1].close
        broke = last > hi if bull else last < lo
        best = ChartPattern(
            name="bull_flag" if bull else "bear_flag",
            direction="bullish" if bull else "bearish",
            status="confirmed" if broke else "forming",
            points=[(p0, pole[0].open), (f0 - 1, pole[-1].close),
                    (f0, hi if bull else lo), (n - 2, lo if bull else hi)],
            neckline=[(f0, hi if bull else lo), (n - 1, hi if bull else lo)],
            target=(hi + abs(move)) if bull else (lo - abs(move)),
        )
    return best


def fibonacci(candles: list[Candle], piv: list[Pivot]) -> Fibonacci | None:
    """Retracement levels on the window's major leg (highest pivot high and
    lowest pivot low, in the order they happened)."""
    highs = [p for p in piv if p.kind == "high"]
    lows = [p for p in piv if p.kind == "low"]
    if not highs or not lows:
        return None
    h = max(highs, key=lambda p: p.price)
    lo = min(lows, key=lambda p: p.price)
    span = h.price - lo.price
    if span <= 0:
        return None
    levels: dict[str, float] = {}
    if lo.index < h.index:   # up leg: retracements measured down from the high
        for r in FIB_RATIOS:
            levels[f"{r:g}"] = h.price - r * span
        for r in FIB_EXTENSIONS:
            levels[f"{r:g}"] = lo.price + r * span
        return Fibonacci("up", (lo.index, lo.price), (h.index, h.price), levels)
    for r in FIB_RATIOS:
        levels[f"{r:g}"] = lo.price + r * span
    for r in FIB_EXTENSIONS:
        levels[f"{r:g}"] = h.price - r * span
    return Fibonacci("down", (h.index, h.price), (lo.index, lo.price), levels)


def find_breakouts(candles: list[Candle], sup: Trendline | None, res: Trendline | None,
                   patterns: list[ChartPattern], zones: Iterable[Any] = ()) -> list[Breakout]:
    """What the latest close broke that the previous close had not."""
    n = len(candles)
    if n < 2:
        return []
    last, prev = candles[-1].close, candles[-2].close
    out: list[Breakout] = []
    if res is not None:
        lvl, plvl = res.price_at(n - 1), res.price_at(n - 2)
        if last > lvl and prev <= plvl:
            out.append(Breakout("resistance_line", "up", lvl))
    if sup is not None:
        lvl, plvl = sup.price_at(n - 1), sup.price_at(n - 2)
        if last < lvl and prev >= plvl:
            out.append(Breakout("support_line", "down", lvl))
    for z in zones:
        if z.kind == "resistance" and last > z.upper >= prev:
            out.append(Breakout("zone", "up", z.upper))
        elif z.kind == "support" and last < z.lower <= prev:
            out.append(Breakout("zone", "down", z.lower))
    for p in patterns:
        if p.status != "confirmed" or not p.neckline:
            continue
        (i1, v1), (i2, v2) = p.neckline
        slope = (v2 - v1) / (i2 - i1) if i2 != i1 else 0.0
        lvl, plvl = v1 + slope * (n - 1 - i1), v1 + slope * (n - 2 - i1)
        up = p.direction == "bullish"
        # Only the bar that crossed counts; a pattern confirmed ten bars ago
        # is not a breakout now.
        if up and prev <= plvl and last > lvl:
            out.append(Breakout(p.name, "up", lvl))
        elif not up and prev >= plvl and last < lvl:
            out.append(Breakout(p.name, "down", lvl))
    return out


def read_chart(candles: list[Candle], zones: Iterable[Any] = ()) -> ChartReading:
    """Everything a trader would draw on these (closed) candles.

    ``zones`` is optional: anything with ``kind``/``lower``/``upper`` (Atlas
    zones) adds zone breakouts.
    """
    if len(candles) < 2 * SWING_LOOKAROUND + 3:
        return ChartReading()
    unit = median_range(candles)
    piv = pivots(candles)
    sup = find_trendline(candles, piv, "support", unit)
    res = find_trendline(candles, piv, "resistance", unit)
    patterns = [p for p in (
        find_double(candles, piv, unit, top=True),
        find_double(candles, piv, unit, top=False),
        find_head_shoulders(candles, piv, unit, inverse=False),
        find_head_shoulders(candles, piv, unit, inverse=True),
        find_flag(candles, unit),
    ) if p is not None]
    return ChartReading(
        pivots=piv, support_line=sup, resistance_line=res,
        formation=classify_formation(sup, res, len(candles)),
        patterns=patterns, fib=fibonacci(candles, piv),
        breakouts=find_breakouts(candles, sup, res, patterns, zones),
        unit=unit,
    )
