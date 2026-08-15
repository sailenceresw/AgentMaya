"""Standard technical indicators: moving averages, volatility, momentum.

Pure functions over a list of candles or closes, each returning ``None`` when
there is not enough history rather than a value computed from a short window.
That distinction matters on a 1m strategy: a 14-period RSI seeded from 4 bars
is not a weak signal, it is a different indicator, and returning it silently
would let the strategy trade on something nobody specified.

Wilder's smoothing (RSI, ATR) is used where the original definition calls for
it rather than a simple mean, so values match what a charting package shows.
"""
from __future__ import annotations

from maya.data.models import Candle


def sma(values: list[float], length: int) -> float | None:
    """Simple moving average of the last ``length`` values."""
    if length < 1 or len(values) < length:
        return None
    return sum(values[-length:]) / length


def ema(values: list[float], length: int) -> float | None:
    """Exponential moving average, seeded with an SMA of the first window."""
    if length < 1 or len(values) < length:
        return None
    k = 2.0 / (length + 1)
    avg = sum(values[:length]) / length
    for v in values[length:]:
        avg = v * k + avg * (1 - k)
    return avg


def true_range(current: Candle, previous: Candle | None) -> float:
    """Wilder's true range: the largest of the bar's own range and the two
    gap-inclusive spans against the previous close."""
    if previous is None:
        return current.high - current.low
    return max(current.high - current.low,
               abs(current.high - previous.close),
               abs(current.low - previous.close))


def atr(candles: list[Candle], length: int = 14) -> float | None:
    """Average True Range, Wilder-smoothed."""
    if len(candles) < length + 1:
        return None
    trs = [true_range(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    value = sum(trs[:length]) / length
    for tr in trs[length:]:
        value = (value * (length - 1) + tr) / length
    return value


def rsi(closes: list[float], length: int = 14) -> float | None:
    """Relative Strength Index, Wilder-smoothed. 0..100."""
    if len(closes) < length + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    if avg_loss == 0:
        # No down moves in the window: RSI is 100 by definition (and dividing
        # would raise), but only if there were up moves at all.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[float, float, float] | None:
    """MACD line, signal line and histogram.

    The signal line is an EMA *of the MACD line*, so it needs a MACD series --
    computing it from closes is a common and silent error that produces a
    plausible-looking number meaning nothing.
    """
    if len(closes) < slow + signal:
        return None
    macd_series: list[float] = []
    for end in range(slow, len(closes) + 1):
        window = closes[:end]
        f, s = ema(window, fast), ema(window, slow)
        if f is None or s is None:
            continue
        macd_series.append(f - s)
    if len(macd_series) < signal:
        return None
    macd_line = macd_series[-1]
    signal_line = ema(macd_series, signal)
    if signal_line is None:
        return None
    return macd_line, signal_line, macd_line - signal_line


def bollinger(closes: list[float], length: int = 20, mult: float = 2.0
              ) -> tuple[float, float, float] | None:
    """Bollinger Bands as ``(upper, middle, lower)``.

    Uses the population standard deviation, which is what the original
    definition specifies (and what charting packages draw).
    """
    if len(closes) < length:
        return None
    window = closes[-length:]
    mid = sum(window) / length
    var = sum((c - mid) ** 2 for c in window) / length
    sd = var ** 0.5
    return mid + mult * sd, mid, mid - mult * sd


def vwap(candles: list[Candle]) -> float | None:
    """Volume-weighted average price over the candles given.

    Uses the typical price (H+L+C)/3 per bar. Note this is a VWAP of whatever
    window is passed, not a session VWAP -- anchor it by passing only the
    session's candles if that is what you want.
    """
    if not candles:
        return None
    total_vol = sum(c.volume for c in candles)
    if total_vol <= 0:
        return None
    return sum((c.high + c.low + c.close) / 3.0 * c.volume
               for c in candles) / total_vol


def supertrend(candles: list[Candle], length: int = 10, mult: float = 3.0
               ) -> tuple[float, str] | None:
    """SuperTrend as ``(level, direction)`` where direction is 'up' or 'down'.

    The bands ratchet: in an uptrend the lower band may only rise, so the stop
    never loosens while the trend holds. Recomputing the raw band each bar
    without that ratchet is the usual mistake and produces a line that whipsaws
    against the price it is supposed to trail.
    """
    if len(candles) < length + 1:
        return None

    direction = "up"
    final_upper = final_lower = None
    # Walk forward from the first bar that has an ATR so the ratchet has the
    # same history it would have had in real time.
    for end in range(length + 1, len(candles) + 1):
        window = candles[:end]
        a = atr(window, length)
        if a is None:
            continue
        c = window[-1]
        mid = (c.high + c.low) / 2.0
        upper, lower = mid + mult * a, mid - mult * a
        prev_close = window[-2].close

        if final_upper is None or final_lower is None:
            final_upper, final_lower = upper, lower
        else:
            final_upper = (upper if upper < final_upper or prev_close > final_upper
                           else final_upper)
            final_lower = (lower if lower > final_lower or prev_close < final_lower
                           else final_lower)

        if c.close > final_upper:
            direction = "up"
        elif c.close < final_lower:
            direction = "down"

    if final_upper is None or final_lower is None:
        return None
    return (final_lower if direction == "up" else final_upper), direction
