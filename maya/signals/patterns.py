"""Candlestick pattern recognition.

Each detector is a pure predicate over the last few candles. Two conventions
run through the module:

- **Patterns are proportional, never absolute.** Every threshold is a ratio of
  body to range or body to body, so the same code reads a $0.30 SOL candle and
  a $65,000 BTC candle identically.
- **Patterns are context-free.** A hammer is a shape; whether it is *bullish*
  depends on the trend it appears in, which is the caller's job (see
  ``maya.signals.technicals``). Detectors that bake in "after a downtrend"
  silently disagree with each other about what a downtrend is.

``detect_patterns`` returns every pattern present, since a bar can legitimately
be both (a bullish engulfing candle is often also a marubozu).
"""
from __future__ import annotations

from dataclasses import dataclass

from maya.data.models import Candle

# A body at or under this fraction of the bar's range counts as "no body".
DOJI_BODY_RATIO = 0.1
# A wick must be at least this multiple of the body to be a hammer/star tail.
LONG_WICK_RATIO = 2.0
# The opposite wick must stay under this fraction of the range.
SHORT_WICK_RATIO = 0.25
# The defining shadow must be at least this share of the whole bar. This, not
# the body size, is what makes a hammer a hammer.
MIN_SHADOW_OF_RANGE = 0.5
# A marubozu's body must fill at least this much of its range.
MARUBOZU_BODY_RATIO = 0.9


@dataclass
class PatternMatch:
    name: str
    bias: str          # 'bullish' | 'bearish' | 'neutral'
    candles: int       # how many bars the pattern spans


def _range(c: Candle) -> float:
    return max(c.high - c.low, 1e-12)


def _body_ratio(c: Candle) -> float:
    return c.body / _range(c)


def is_doji(c: Candle) -> bool:
    """Open and close effectively equal: indecision."""
    return _body_ratio(c) <= DOJI_BODY_RATIO


def is_marubozu(c: Candle) -> bool:
    """Body fills the range: one side controlled the whole bar."""
    return _body_ratio(c) >= MARUBOZU_BODY_RATIO


def is_hammer(c: Candle) -> bool:
    """Long lower wick, small body near the high — sellers pushed and failed.

    Shape only: identical geometry at the top of a rally is a 'hanging man'.

    Note this deliberately does *not* exclude doji-sized bodies. A hammer is
    defined by its lower shadow, and a hammer with almost no body is a
    dragonfly doji — the same rejection, read the same way. Excluding it would
    make the textbook hammer (tiny body, huge lower wick) undetectable, so
    both patterns are allowed to report on the same bar.
    """
    body = max(c.body, 1e-12)
    return (c.lower_wick >= LONG_WICK_RATIO * body
            and c.lower_wick >= MIN_SHADOW_OF_RANGE * _range(c)
            and c.upper_wick <= SHORT_WICK_RATIO * _range(c))


def is_shooting_star(c: Candle) -> bool:
    """Long upper wick, small body near the low — buyers pushed and failed."""
    body = max(c.body, 1e-12)
    return (c.upper_wick >= LONG_WICK_RATIO * body
            and c.upper_wick >= MIN_SHADOW_OF_RANGE * _range(c)
            and c.lower_wick <= SHORT_WICK_RATIO * _range(c))


def is_bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    """A down bar fully swallowed by the next up bar's body."""
    return (not prev.is_bullish and cur.is_bullish
            and cur.close >= prev.open and cur.open <= prev.close
            and cur.body > prev.body)


def is_bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    return (prev.is_bullish and not cur.is_bullish
            and cur.open >= prev.close and cur.close <= prev.open
            and cur.body > prev.body)


def is_morning_star(a: Candle, b: Candle, c: Candle) -> bool:
    """Three-bar bottom reversal: strong down bar, small indecisive bar, strong
    up bar closing back into the first bar's body.

    This is what "rising star" usually refers to — the bullish counterpart of
    the evening star.
    """
    return (not a.is_bullish and a.body > 0
            and b.body < a.body * 0.5
            and c.is_bullish
            and c.close > (a.open + a.close) / 2.0)


def is_evening_star(a: Candle, b: Candle, c: Candle) -> bool:
    """Three-bar top reversal — the bearish mirror of the morning star."""
    return (a.is_bullish and a.body > 0
            and b.body < a.body * 0.5
            and not c.is_bullish
            and c.close < (a.open + a.close) / 2.0)


def is_inside_bar(prev: Candle, cur: Candle) -> bool:
    """Range contained by the previous bar: compression, often pre-breakout."""
    return cur.high <= prev.high and cur.low >= prev.low


def detect_patterns(candles: list[Candle]) -> list[PatternMatch]:
    """Every pattern completing on the most recent candle."""
    out: list[PatternMatch] = []
    if not candles:
        return out

    cur = candles[-1]
    if is_doji(cur):
        out.append(PatternMatch("doji", "neutral", 1))
    if is_marubozu(cur):
        out.append(PatternMatch("marubozu", "bullish" if cur.is_bullish else "bearish", 1))
    if is_hammer(cur):
        out.append(PatternMatch("hammer", "bullish", 1))
    if is_shooting_star(cur):
        out.append(PatternMatch("shooting_star", "bearish", 1))

    if len(candles) >= 2:
        prev = candles[-2]
        if is_bullish_engulfing(prev, cur):
            out.append(PatternMatch("bullish_engulfing", "bullish", 2))
        if is_bearish_engulfing(prev, cur):
            out.append(PatternMatch("bearish_engulfing", "bearish", 2))
        if is_inside_bar(prev, cur):
            out.append(PatternMatch("inside_bar", "neutral", 2))

    if len(candles) >= 3:
        a, b, c = candles[-3], candles[-2], candles[-1]
        if is_morning_star(a, b, c):
            out.append(PatternMatch("morning_star", "bullish", 3))
        if is_evening_star(a, b, c):
            out.append(PatternMatch("evening_star", "bearish", 3))
    return out


def pattern_bias(matches: list[PatternMatch]) -> str:
    """Net directional read across the patterns present.

    Ties resolve to 'neutral' rather than to a side — two patterns pointing
    opposite ways is genuinely no information, and picking one would
    manufacture a signal out of a disagreement.
    """
    bull = sum(1 for m in matches if m.bias == "bullish")
    bear = sum(1 for m in matches if m.bias == "bearish")
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"
