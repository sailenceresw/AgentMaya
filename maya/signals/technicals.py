"""Classical chart reading on a higher-timeframe candle series.

This is the "what does the chart say" layer: trend by moving averages, market
structure by swing points, and the nearest support/resistance. It is
deliberately small and mechanical — every output is reproducible from the
candles, with no discretionary judgement to drift between backtest and live.

Read together, trend and structure answer different questions and can
legitimately disagree: EMAs describe where price *has been* pulled, structure
describes whether it is still making progress. A trend that is up while
structure has broken down is an early warning, and the disagreement is
information, so neither is collapsed into the other.
"""
from __future__ import annotations

from maya.data.models import Candle
from maya.signals.models import HTFContext

# EMA lengths. 9/21 is the conventional fast/slow scalping pair and is short
# enough to say something useful from the ~30 higher-timeframe candles a few
# days of 1m data produces.
EMA_FAST = 9
EMA_SLOW = 21
# How far apart the EMAs must be, as a fraction of price, before the trend is
# called rather than left neutral. Without a band, EMAs that are effectively
# touching flip the trend every bar and the signal becomes noise.
TREND_BAND = 0.0008  # 0.08%
# Bars either side of a candle that must be lower/higher for it to be a swing.
SWING_LOOKAROUND = 2


def ema(values: list[float], length: int) -> float | None:
    """Exponential moving average of the last ``length`` periods.

    Seeded with a simple average of the first ``length`` values rather than
    with values[0], so a single unrepresentative first candle cannot bias the
    whole series.
    """
    if len(values) < length:
        return None
    k = 2.0 / (length + 1)
    avg = sum(values[:length]) / length
    for v in values[length:]:
        avg = v * k + avg * (1 - k)
    return avg


def swing_points(candles: list[Candle], lookaround: int = SWING_LOOKAROUND
                 ) -> tuple[list[float], list[float]]:
    """Confirmed swing highs and lows.

    A swing point needs ``lookaround`` candles on *both* sides, so the most
    recent few candles can never form one. That lag is not a defect: a swing
    high is only a swing high once price has failed to exceed it, and calling
    one earlier would be reading the future.
    """
    highs: list[float] = []
    lows: list[float] = []
    n = len(candles)
    for i in range(lookaround, n - lookaround):
        window = candles[i - lookaround: i + lookaround + 1]
        c = candles[i]
        if c.high == max(w.high for w in window) and c.high > candles[i - 1].high:
            highs.append(c.high)
        if c.low == min(w.low for w in window) and c.low < candles[i - 1].low:
            lows.append(c.low)
    return highs, lows


def classify_structure(highs: list[float], lows: list[float]) -> str:
    """Market structure from the last two confirmed swings on each side."""
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    lh, ll = highs[-1] < highs[-2], lows[-1] < lows[-2]
    if hh and hl:
        return "higher_highs"      # uptrend intact
    if lh and ll:
        return "lower_lows"        # downtrend intact
    return "range"                 # mixed: no directional progress


def analyze(candles: list[Candle], timeframe: str) -> HTFContext:
    """Read one higher-timeframe series into an :class:`HTFContext`.

    ``candles`` must be *closed* candles only — see
    ``maya.signals.timeframes.TimeframeAggregator``.
    """
    ctx = HTFContext(timeframe=timeframe, candles=len(candles))
    if not candles:
        return ctx

    closes = [c.close for c in candles]
    price = closes[-1]
    ctx.price = price
    ctx.ema_fast = ema(closes, EMA_FAST)
    ctx.ema_slow = ema(closes, EMA_SLOW)

    if ctx.ema_fast is not None and ctx.ema_slow is not None and price:
        spread = (ctx.ema_fast - ctx.ema_slow) / price
        if spread > TREND_BAND:
            ctx.trend = "up"
        elif spread < -TREND_BAND:
            ctx.trend = "down"
        else:
            ctx.trend = "neutral"

    highs, lows = swing_points(candles)
    ctx.structure = classify_structure(highs, lows)
    # Nearest untested level on each side — what a chart reader would draw.
    above = [h for h in highs if h > price]
    below = [lo for lo in lows if lo < price]
    ctx.resistance = min(above) if above else None
    ctx.support = max(below) if below else None
    return ctx


def supports(ctx: HTFContext, direction: str) -> bool:
    """Does this timeframe back a trade in ``direction``?"""
    return ctx.trend == ("up" if direction == "long" else "down")


def targets_liquidity(contexts: dict[str, HTFContext], direction: str,
                      entry: float, target: float,
                      tolerance: float = 0.0015) -> bool:
    """Does the trade aim at a pool of resting liquidity?

    **This is a proxy, not liquidation data.** Genuine liquidation-cluster
    feeds (open interest by price, exchange liquidation streams) are not
    available here, and inventing them would be worse than approximating them
    openly. What is used instead is the structural fact the concept rests on:
    stops and liquidations accumulate just beyond prior swing highs and lows,
    so a target sitting on the far side of one is aiming at where the fuel is.

    Returns True when ``target`` reaches a confirmed higher-timeframe swing
    level in the direction of travel, within ``tolerance`` of it as a fraction
    of price.
    """
    if not contexts or not entry:
        return False
    for ctx in contexts.values():
        level = ctx.resistance if direction == "long" else ctx.support
        if level is None:
            continue
        reached = target >= level if direction == "long" else target <= level
        near = abs(target - level) / entry <= tolerance
        if reached or near:
            return True
    return False


def aligned(contexts: dict[str, HTFContext], direction: str) -> bool:
    """Multi-timeframe alignment: every configured higher timeframe agrees.

    Requiring unanimity is the point — the reason to look at more than one
    chart is to be stopped when they disagree. This only ever *adds* a
    confluence point, so strictness here cannot block a trade on its own.
    """
    if not contexts:
        return False
    return all(supports(c, direction) for c in contexts.values())
