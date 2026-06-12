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

    # expected_rr must reflect the EXECUTED target (TP1), since the position is
    # closed in full at TP1 — not the further TP2. Otherwise the risk gate
    # screens against an inflated R:R the trade never realises.
    if ab.side == "bullish" and at_value(vp.val):
        entry = price
        sl = ab.level * (1 - 0.003)
        tp1 = vp.poc
        tp2 = vp.vah
        if _rr(entry, sl, tp1) >= 4.0:
            return TradeIntent(sig.symbol, "long", "value_area_absorption",
                               entry, sl, tp1, tp2, 0, [], _rr(entry, sl, tp1), sig.timestamp)

    if ab.side == "bearish" and at_value(vp.vah):
        entry = price
        sl = ab.level * (1 + 0.003)
        tp1 = vp.poc
        tp2 = vp.val
        if _rr(entry, sl, tp1) >= 4.0:
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
    if ctx.swept_high and sig.cvd_1m < 0:
        entry = price
        sl = sig.high * (1 + 0.004)
        tp1 = ctx.swing_low_15m
        if _rr(entry, sl, tp1) >= 5.0:
            return TradeIntent(sig.symbol, "short", "liquidity_sweep_choch",
                               entry, sl, tp1, tp1, 0, [], _rr(entry, sl, tp1), sig.timestamp)

    # Low sweep: sellers' breakdown absorbed, buyers regain control (cvd > 0).
    if ctx.swept_low and sig.cvd_1m > 0:
        entry = price
        sl = sig.low * (1 - 0.004)
        tp1 = ctx.swing_high_15m
        if _rr(entry, sl, tp1) >= 5.0:
            return TradeIntent(sig.symbol, "long", "liquidity_sweep_choch",
                               entry, sl, tp1, tp1, 0, [], _rr(entry, sl, tp1), sig.timestamp)
    return None


def compression_breakout(sig: SignalObject, ctx: SetupContext) -> TradeIntent | None:
    """Setup 3 — trade the expansion out of a tight compression range."""
    compressed = ctx.range_pct < 0.003
    vol_expansion = sig.volume > 3.0 * sig.avg_volume
    if not (compressed and vol_expansion):
        return None
    price = sig.price
    rng = (ctx.swing_high_15m - ctx.swing_low_15m)
    mid = (ctx.swing_high_15m + ctx.swing_low_15m) / 2
    # Measured-move target. The spec says "range height x 2 minimum"; with a
    # mid-range stop a 2x target caps R:R near 4:1, which can't satisfy the
    # 1:5 minimum, so use a larger measured move. The R:R gate below then only
    # admits breakouts that close near the range top (small risk).
    move = rng * 3.5

    if ctx.breakout_up and sig.cvd_1m > 0:
        entry = price
        sl = mid
        tp1 = price + move
        if _rr(entry, sl, tp1) >= 5.0:
            return TradeIntent(sig.symbol, "long", "compression_breakout",
                               entry, sl, tp1, tp1, 0, [], _rr(entry, sl, tp1), sig.timestamp)

    if ctx.breakout_down and sig.cvd_1m < 0:
        entry = price
        sl = mid
        tp1 = price - move
        if _rr(entry, sl, tp1) >= 5.0:
            return TradeIntent(sig.symbol, "short", "compression_breakout",
                               entry, sl, tp1, tp1, 0, [], _rr(entry, sl, tp1), sig.timestamp)
    return None


ALL_SETUPS = (value_area_absorption, liquidity_sweep_choch, compression_breakout)
