"""Echo — candlestick patterns, read in context.

The pattern library itself is context-free by design (see
``maya.signals.patterns``): a hammer is a shape. Echo is where that shape
acquires meaning, because the same candle says completely different things
depending on where it prints.

A hammer at a support zone is a rejection of lower prices by buyers who were
already defending that level. The identical hammer in the middle of a range is
a bar with a long lower wick. Most retail pattern trading fails on exactly this
distinction, so Echo scores the location, not just the shape.
"""
from __future__ import annotations

from maya.agents.atlas import Levels
from maya.agents.models import BEARISH, BULLISH, NEUTRAL, Finding
from maya.data.models import Candle
from maya.signals import patterns as pat

AGENT = "Echo"

# A pattern with no supporting location earns this much on its own. Low on
# purpose: shape alone is weak evidence, which the measurement supports —
# pattern_bullish/bearish scored 3/8 to 6/8 sign consistency over 24 months.
BASE_CONFIDENCE = 0.25
# Bonus when the pattern prints inside a zone that agrees with its direction.
CONTEXT_BONUS = 0.45
# Penalty when it prints inside a zone that contradicts it (a bullish hammer
# right under heavy resistance is a trap more often than a signal).
CONTEXT_PENALTY = 0.15


def observe(candles: list[Candle], levels: Levels | None = None) -> Finding:
    """Echo's read: which patterns completed, and whether location backs them."""
    if not candles:
        return Finding(AGENT, NEUTRAL, 0.0, "no candles")

    matches = pat.detect_patterns(candles)
    if not matches:
        return Finding(AGENT, NEUTRAL, 0.0, "no pattern on this bar",
                       {"patterns": []})

    names = [m.name for m in matches]
    bias = pat.pattern_bias(matches)
    data = {"patterns": names, "bias": bias}

    if bias == NEUTRAL:
        return Finding(AGENT, NEUTRAL, 0.0,
                       f"patterns disagree or are neutral: {', '.join(names)}",
                       data)

    bar = candles[-1]
    zone = None
    if levels:
        # By the bar's range, not its close: a rejection candle tests the level
        # with its wick and closes away from it. Ask for the side that would
        # *support* this pattern first — a wide bar can graze both, and judging
        # a hammer that tagged support against a resistance zone it merely
        # brushed would invert the reading for no good reason.
        confirming = "support" if bias == BULLISH else "resistance"
        zone = (levels.zone_touching(bar.low, bar.high, kind=confirming)
                or levels.zone_touching(bar.low, bar.high))
    confidence = BASE_CONFIDENCE
    where = "mid-range"

    if zone is not None:
        agrees = ((bias == BULLISH and zone.kind == "support")
                  or (bias == BEARISH and zone.kind == "resistance"))
        if agrees:
            # Scale the bonus by how well-established the zone is: a pattern at
            # a four-touch shelf is worth more than one at a single wick.
            confidence += CONTEXT_BONUS * zone.strength
            where = f"at {zone.kind} {zone.center:.2f} (strength {zone.strength:.2f})"
        else:
            confidence = max(0.0, confidence - CONTEXT_PENALTY)
            where = f"against {zone.kind} {zone.center:.2f}"
        data["zone"] = zone.kind
        data["zone_strength"] = zone.strength

    return Finding(AGENT, bias, round(min(confidence, 1.0), 4),
                   f"{', '.join(names)} {where}", data)
