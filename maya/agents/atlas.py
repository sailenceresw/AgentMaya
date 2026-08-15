"""Atlas — support, resistance and market structure.

A level is not a line, it is a region price has repeatedly cared about. The
previous implementation returned "the nearest swing high above price", which
treats a single wick as equal to a shelf price has bounced off five times.
Atlas clusters swing points into **zones** and scores each by how much
evidence stands behind it:

- **touches** — how many separate swings landed in the zone. One touch is a
  coincidence; four is a level other participants are also watching.
- **volume** — traded volume on the touching bars, so a zone defended on heavy
  volume outranks one brushed in thin trade.
- **recency** — a shelf from three months ago matters less than last week's.

Zones are built from *confirmed* swings only (see ``technicals.swing_points``),
so nothing here can see a pivot before price has finished making it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from maya.agents.models import BEARISH, BULLISH, NEUTRAL, Finding
from maya.data.models import Candle
from maya.signals import technicals as ta

AGENT = "Atlas"

# Swings within this fraction of price collapse into one zone. Too tight and
# every wick is its own level; too loose and support and resistance merge into
# a single meaningless band.
ZONE_TOLERANCE = 0.0020          # 0.20%
# A zone must be at least this close to price to be "in play".
PROXIMITY = 0.0035               # 0.35%
MIN_TOUCHES_FOR_STRENGTH = 3     # touches at which a zone scores full marks


@dataclass
class Zone:
    """A price region with repeated historical reaction."""

    center: float
    lower: float
    upper: float
    touches: int
    volume: float
    last_ts: int
    kind: str                    # 'support' | 'resistance'
    strength: float = 0.0        # 0..1

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def distance_pct(self, price: float) -> float:
        return abs(self.center - price) / price if price else float("inf")


@dataclass
class Levels:
    """All zones Atlas currently sees, nearest-first on each side."""

    support: list[Zone] = field(default_factory=list)
    resistance: list[Zone] = field(default_factory=list)

    def nearest_support(self, price: float) -> Zone | None:
        below = [z for z in self.support if z.center <= price]
        return max(below, key=lambda z: z.center) if below else None

    def nearest_resistance(self, price: float) -> Zone | None:
        above = [z for z in self.resistance if z.center >= price]
        return min(above, key=lambda z: z.center) if above else None

    def zone_at(self, price: float) -> Zone | None:
        """The zone price is currently sitting inside, strongest first."""
        inside = [z for z in self.support + self.resistance if z.contains(price)]
        return max(inside, key=lambda z: z.strength) if inside else None

    def zone_touching(self, low: float, high: float,
                      kind: str | None = None) -> Zone | None:
        """The strongest zone the bar's *range* intersects.

        A rejection candle tests a level with its wick and closes away from it —
        that is the whole shape. Judging by the close alone would miss exactly
        the bars that matter most, so pattern context asks whether the bar
        reached the zone, not where it finished.

        A wide bar can touch support and resistance at once. ``kind`` lets the
        caller ask for the side relevant to its own read first, so a bullish
        pattern that tagged support is not scored against a resistance zone the
        same bar happened to graze.
        """
        hit = [z for z in self.support + self.resistance
               if low <= z.upper and high >= z.lower
               and (kind is None or z.kind == kind)]
        return max(hit, key=lambda z: z.strength) if hit else None


def _cluster(points: list[tuple[float, int, float]], kind: str,
             tolerance: float) -> list[Zone]:
    """Group (price, ts, volume) swing points into zones.

    Greedy over price-sorted points: walk upward, extending the current zone
    while the next point is within tolerance of the zone's *centre*. Comparing
    against the centre rather than the previous point stops a chain of
    near-misses from drifting into one enormous band.
    """
    if not points:
        return []
    points = sorted(points)
    zones: list[Zone] = []
    bucket = [points[0]]

    def flush(items: list[tuple[float, int, float]]) -> None:
        prices = [p for p, _, _ in items]
        centre = sum(prices) / len(prices)
        # A zone is a region, not a line. Touches that all land on the exact
        # same print would otherwise produce a zero-width zone that price can
        # never be "inside", which is the opposite of what repeated touches at
        # one price mean. Pad to at least half the clustering tolerance.
        pad = centre * tolerance / 2
        zones.append(Zone(
            center=centre,
            lower=min(min(prices), centre - pad),
            upper=max(max(prices), centre + pad),
            touches=len(items), volume=sum(v for _, _, v in items),
            last_ts=max(t for _, t, _ in items), kind=kind,
        ))

    for price, ts, vol in points[1:]:
        centre = sum(p for p, _, _ in bucket) / len(bucket)
        if abs(price - centre) / max(centre, 1e-9) <= tolerance:
            bucket.append((price, ts, vol))
        else:
            flush(bucket)
            bucket = [(price, ts, vol)]
    flush(bucket)
    return zones


def _score(zones: list[Zone], now_ts: int, span_ms: int) -> None:
    """Assign 0..1 strength from touches, volume share and recency."""
    if not zones:
        return
    max_vol = max(z.volume for z in zones) or 1.0
    for z in zones:
        touch_score = min(z.touches / MIN_TOUCHES_FOR_STRENGTH, 1.0)
        vol_score = z.volume / max_vol
        # Linear decay across the window; a zone touched at the very start of
        # the sample scores ~0 for recency, one touched now scores ~1.
        age = (now_ts - z.last_ts) / span_ms if span_ms > 0 else 0.0
        recency = max(0.0, 1.0 - age)
        z.strength = round(0.5 * touch_score + 0.3 * vol_score + 0.2 * recency, 4)


def find_levels(candles: list[Candle], tolerance: float = ZONE_TOLERANCE) -> Levels:
    """Cluster confirmed swings into scored support and resistance zones."""
    if len(candles) < 5:
        return Levels()

    highs, lows = ta.swing_points(candles)
    # swing_points returns prices only, so re-walk the candles to attach the
    # timestamp and volume each level was made on.
    high_pts: list[tuple[float, int, float]] = []
    low_pts: list[tuple[float, int, float]] = []
    hset, lset = set(highs), set(lows)
    for c in candles:
        if c.high in hset:
            high_pts.append((c.high, c.timestamp, c.volume))
        if c.low in lset:
            low_pts.append((c.low, c.timestamp, c.volume))

    now_ts = candles[-1].timestamp
    span = max(now_ts - candles[0].timestamp, 1)
    resistance = _cluster(high_pts, "resistance", tolerance)
    support = _cluster(low_pts, "support", tolerance)
    _score(resistance, now_ts, span)
    _score(support, now_ts, span)
    return Levels(support=support, resistance=resistance)


def observe(candles: list[Candle], levels: Levels | None = None) -> Finding:
    """Atlas's read: where price sits relative to the levels that matter."""
    if len(candles) < 5:
        return Finding(AGENT, NEUTRAL, 0.0, "not enough history for levels")

    levels = levels if levels is not None else find_levels(candles)
    price = candles[-1].close
    sup = levels.nearest_support(price)
    res = levels.nearest_resistance(price)

    data = {
        "support": sup.center if sup else None,
        "support_strength": sup.strength if sup else 0.0,
        "support_touches": sup.touches if sup else 0,
        "resistance": res.center if res else None,
        "resistance_strength": res.strength if res else 0.0,
        "resistance_touches": res.touches if res else 0,
        "structure": ta.classify_structure(*ta.swing_points(candles)),
    }

    # Sitting on a level is the informative case: at support, downside is
    # defended; at resistance, upside is capped.
    at = levels.zone_at(price)
    if at is not None:
        verdict = BULLISH if at.kind == "support" else BEARISH
        return Finding(
            AGENT, verdict, at.strength,
            f"price at {at.kind} zone {at.center:.2f} "
            f"({at.touches} touches, strength {at.strength:.2f})",
            data | {"in_zone": at.kind, "zone_center": at.center},
        )

    # Otherwise lean toward whichever side has more room before it hits a wall.
    if sup and res:
        room_up = (res.center - price) / price
        room_down = (price - sup.center) / price
        total = room_up + room_down
        if total > 0:
            skew = (room_up - room_down) / total    # +1 all room above
            if abs(skew) >= 0.4:
                verdict = BULLISH if skew > 0 else BEARISH
                return Finding(
                    AGENT, verdict, round(min(abs(skew), 1.0) * 0.6, 4),
                    f"room to {'resistance' if skew > 0 else 'support'}: "
                    f"{max(room_up, room_down) * 100:.2f}% vs "
                    f"{min(room_up, room_down) * 100:.2f}%",
                    data,
                )
    return Finding(AGENT, NEUTRAL, 0.0, "price mid-range between levels", data)
