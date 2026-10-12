"""The three production setups (spec section 6.2).

Each setup is a pure function: given the current :class:`SignalObject` (and a
little rolling context), it returns a *candidate* :class:`TradeIntent` or
``None``. Candidates are later scored for confluence and screened by risk.
"""
from __future__ import annotations

from dataclasses import dataclass

from maya.signals.models import SignalObject
from maya.strategy.models import TradeIntent


@dataclass
class SetupContext:
    """Light rolling context the setups need beyond a single signal."""

    swing_high_15m: float
    swing_low_15m: float
    range_pct: float
    swept_high: bool = False
    swept_low: bool = False
    breakout_up: bool = False
    breakout_down: bool = False


# Minimum reward:risk each setup demands of its own geometry, before the risk
# layer applies RiskLimits.MIN_RR_FOR_ENTRY on top. Named rather than inline so
# they can be calibrated against measured R:R distributions instead of guessed.
RR_MIN_VALUE_AREA_ABSORPTION = 4.0
RR_MIN_LIQUIDITY_SWEEP = 5.0
RR_MIN_COMPRESSION_BREAKOUT = 5.0

# How far beyond the invalidation level a stop sits, as a multiple of ATR.
# Previously these buffers were flat percentages (0.3% / 0.4%) — roughly 4x
# BTC's typical 1m ATR — which put the stop so far away that the measured R:R
# of a value-area fade was 0.15 against a required 4.0. It could not pass, ever,
# regardless of entry quality. Scaling by ATR makes the buffer mean the same
# thing across symbols and volatility regimes.
STOP_ATR_MULT = 0.5
# Used only when ATR is unavailable (cold start, <15 candles). Kept small for
# the same reason the ATR multiple is: a wide fallback silently recreates the
# original defect during warm-up.
STOP_FALLBACK_PCT = 0.0008


def _stop_buffer(price: float, atr: float | None) -> float:
    """Absolute price distance to place a stop beyond its invalidation level."""
    if atr is not None and atr > 0:
        return STOP_ATR_MULT * atr
    return price * STOP_FALLBACK_PCT


def _rr(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    return abs(tp - entry) / risk if risk else 0.0


def value_area_absorption(sig: SignalObject, ctx: SetupContext) -> TradeIntent | None:
    """Setup 1 — fade absorption at value-area extremes."""
    vp = sig.volume_profile
    ab = sig.absorption
    if vp is None or not ab.detected or ab.level is None:
        return None
    price = sig.price
    # The absorption must occur at a value-area extreme (spec 6.2 Setup 1).
    at_value = lambda boundary: abs(ab.level - boundary) / price <= 0.0015

    buffer = _stop_buffer(price, sig.atr)
    if ab.side == "bullish" and at_value(vp.val):
        entry = price
        sl = ab.level - buffer
        tp1 = vp.poc
        tp2 = vp.vah
        if _rr(entry, sl, tp1) >= RR_MIN_VALUE_AREA_ABSORPTION:
            # R:R to TP1: the engine exits the whole position there, so a
            # TP2 figure here overstated the edge the risk gate approved.
            return TradeIntent(sig.symbol, "long", "value_area_absorption",
                               entry, sl, tp1, tp2, 0, [], _rr(entry, sl, tp1), sig.timestamp)

    if ab.side == "bearish" and at_value(vp.vah):
        entry = price
        sl = ab.level + buffer
        tp1 = vp.poc
        tp2 = vp.val
        if _rr(entry, sl, tp1) >= RR_MIN_VALUE_AREA_ABSORPTION:
            return TradeIntent(sig.symbol, "short", "value_area_absorption",
                               entry, sl, tp1, tp2, 0, [], _rr(entry, sl, tp1), sig.timestamp)
    return None


def liquidity_sweep_choch(sig: SignalObject, ctx: SetupContext) -> TradeIntent | None:
    """Setup 2 — fade a liquidity sweep confirmed by a change of character."""
    vol_spike = sig.volume > 2.0 * sig.avg_volume
    if not vol_spike:
        return None
    price = sig.price

    # High sweep: buyers' breakout absorbed, sellers regain control (cvd < 0).
    buffer = _stop_buffer(price, sig.atr)
    if ctx.swept_high and sig.cvd_1m < 0:
        entry = price
        sl = sig.high + buffer
        tp1 = ctx.swing_low_15m
        if _rr(entry, sl, tp1) >= RR_MIN_LIQUIDITY_SWEEP:
            return TradeIntent(sig.symbol, "short", "liquidity_sweep_choch",
                               entry, sl, tp1, tp1, 0, [], _rr(entry, sl, tp1), sig.timestamp)

    # Low sweep: sellers' breakdown absorbed, buyers regain control (cvd > 0).
    if ctx.swept_low and sig.cvd_1m > 0:
        entry = price
        sl = sig.low - buffer
        tp1 = ctx.swing_high_15m
        if _rr(entry, sl, tp1) >= RR_MIN_LIQUIDITY_SWEEP:
            return TradeIntent(sig.symbol, "long", "liquidity_sweep_choch",
                               entry, sl, tp1, tp1, 0, [], _rr(entry, sl, tp1), sig.timestamp)
    return None


# Spec 6.2 Setup 3 limits.
COMPRESSION_MAX_RANGE = 0.003      # range < 0.3% ...
COMPRESSION_MAX_STOP = 0.005       # stop mid-range, max 0.5% from entry
COMPRESSION_MIN_BODY = 0.5         # breakout candle body > 50% of its range
COMPRESSION_TARGET_RANGES = 2.0    # measured move: range height x 2 minimum


def compression_breakout(sig: SignalObject, ctx: SetupContext) -> TradeIntent | None:
    """Setup 3 — trade the expansion out of a tight compression range.

    Built to spec 6.2: a sub-0.3% range over the prior 30 candles, a volume
    expansion (>3x average), a CVD push in the breakout direction, and a
    close beyond the range with a body over half the candle. The stop is
    mid-range and may be at most 0.5% from entry.

    The spec's target ("range height x 2 *minimum*") and its 1:5 minimum R:R
    conflict: with the stop at mid-range the risk is at least half the range,
    so a 2x target caps R:R at 1:4 and the setup could never fire. Since 2x is
    stated as a minimum, the target is extended to whichever is further — 2x
    the range or 5x the risk — which honours both. That makes the target
    further than a textbook measured move, so whether the setup earns its
    keep is a question for the backtest, not for this function.
    """
    if sig.range_high is None or sig.range_low is None:
        return None
    if sig.range_pct >= COMPRESSION_MAX_RANGE or sig.volume <= 3.0 * sig.avg_volume:
        return None
    candle_range = sig.high - sig.low
    if candle_range <= 0 or abs(sig.price - sig.open) / candle_range <= COMPRESSION_MIN_BODY:
        return None

    price = sig.price
    rng = sig.range_high - sig.range_low
    mid = (sig.range_high + sig.range_low) / 2

    if price > sig.range_high and sig.cvd_1m > 0:
        direction, sign = "long", 1
    elif price < sig.range_low and sig.cvd_1m < 0:
        direction, sign = "short", -1
    else:
        return None

    risk = abs(price - mid)
    if risk <= 0 or risk / price > COMPRESSION_MAX_STOP:
        return None
    reach = max(COMPRESSION_TARGET_RANGES * rng, RR_MIN_COMPRESSION_BREAKOUT * risk)
    tp1 = price + sign * reach
    return TradeIntent(sig.symbol, direction, "compression_breakout",
                       price, mid, tp1, tp1, 0, [], _rr(price, mid, tp1), sig.timestamp)


ALL_SETUPS = (value_area_absorption, liquidity_sweep_choch, compression_breakout)
