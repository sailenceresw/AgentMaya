"""Pure indicator functions used by the signal processor.

These implement the order-flow calculations from spec section 5.2:
CVD, volume profile (VAH/POC/VAL), absorption detection, CVD divergence and
Auction Market Theory phase classification.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from maya.data.models import Candle, Trade
from maya.signals.models import Absorption, VolumeProfile


def candle_delta(trades: list[Trade]) -> float:
    """Cumulative Volume Delta for one candle (spec 5.2.1)."""
    buy = sum(t.qty for t in trades if not t.is_buyer_maker)
    sell = sum(t.qty for t in trades if t.is_buyer_maker)
    return buy - sell


def volume_profile(candles: list[Candle], bins: int = 24, value_area: float = 0.70) -> VolumeProfile:
    """Volume profile over a rolling window (spec 5.2.2).

    Distributes each candle's volume across price bins, then derives POC and
    the value area covering ``value_area`` (70%) of total volume.
    """
    if not candles:
        return VolumeProfile(0, 0, 0, "trend")

    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if hi <= lo:
        return VolumeProfile(hi, hi, hi, "trend")

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(bins)
    for c in candles:
        # spread candle volume across the bins its range spans
        span_lo = np.searchsorted(edges, c.low, side="right") - 1
        span_hi = np.searchsorted(edges, c.high, side="right") - 1
        span_lo = max(0, min(span_lo, bins - 1))
        span_hi = max(0, min(span_hi, bins - 1))
        n = span_hi - span_lo + 1
        vol[span_lo:span_hi + 1] += c.volume / n

    poc_idx = int(np.argmax(vol))
    poc = float(centers[poc_idx])

    # Grow value area outward from POC until it covers `value_area` of volume.
    total = vol.sum()
    target = total * value_area
    lo_i = hi_i = poc_idx
    covered = vol[poc_idx]
    while covered < target and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            covered += vol[hi_i]
        else:
            lo_i -= 1
            covered += vol[lo_i]

    val = float(centers[lo_i])
    vah = float(centers[hi_i])

    # crude shape classification
    width = (hi_i - lo_i + 1) / bins
    if poc_idx > 0.66 * bins or poc_idx < 0.33 * bins:
        shape = "P" if poc_idx > 0.5 * bins else "b"
    elif width < 0.35:
        shape = "trend"
    else:
        shape = "D"
    return VolumeProfile(vah=vah, poc=poc, val=val, shape=shape)


def detect_absorption(candles: list[Candle], cluster_pct: float = 0.0015,
                      min_wicks: int = 3, wick_body_ratio: float = 1.5) -> Absorption:
    """Detect absorption: clustered high-volume wicks with no progress (spec 5.2.3).

    ``cluster_pct`` is the price tolerance for treating wicks as being at the
    "same level", expressed as a fraction of price (so it scales with BTC's
    absolute price instead of a fixed tick).
    """
    if len(candles) < min_wicks:
        return Absorption()

    recent = candles[-6:]
    avg_vol = np.mean([c.volume for c in recent]) or 1.0
    ref_price = recent[-1].close or 1.0

    lower_levels: list[tuple[float, float]] = []  # (level, strength)
    upper_levels: list[tuple[float, float]] = []
    for c in recent:
        body = max(c.body, 1e-9)
        if c.lower_wick / body >= wick_body_ratio and c.volume > avg_vol:
            lower_levels.append((c.low, c.volume / avg_vol))
        if c.upper_wick / body >= wick_body_ratio and c.volume > avg_vol:
            upper_levels.append((c.high, c.volume / avg_vol))

    def cluster(levels: list[tuple[float, float]], window: float):
        if len(levels) < min_wicks:
            return None
        prices = np.array([p for p, _ in levels])
        for ref, _ in levels:
            members = [(p, s) for p, s in levels if abs(p - ref) <= window]
            if len(members) >= min_wicks:
                lvl = float(np.mean([p for p, _ in members]))
                strength = float(min(1.0, np.mean([s for _, s in members]) / 3.0))
                return lvl, strength
        return None

    window = ref_price * cluster_pct
    bull = cluster(lower_levels, window)
    bear = cluster(upper_levels, window)

    if bull and (not bear or bull[1] >= bear[1]):
        return Absorption(detected=True, side="bullish", level=bull[0], strength=bull[1])
    if bear:
        return Absorption(detected=True, side="bearish", level=bear[0], strength=bear[1])
    return Absorption()


def _swings(values: list[float]) -> tuple[float, float]:
    """Return (last-low, last-high) of a short window."""
    arr = np.asarray(values[-10:]) if values else np.array([0.0])
    return float(arr.min()), float(arr.max())


def cvd_divergence(prices: list[float], cvds: list[float]) -> str | None:
    """Classify CVD divergence vs price (spec 5.2.4)."""
    if len(prices) < 6:
        return None
    p_now, p_prev = prices[-1], prices[-6]
    c_now, c_prev = cvds[-1], cvds[-6]
    price_up = p_now > p_prev
    cvd_up = c_now > c_prev
    if not price_up and cvd_up:
        return "bullish"          # price LL, CVD HL
    if price_up and not cvd_up:
        return "bearish"          # price HH, CVD LH
    return None


def amt_phase(candles: list[Candle], cvds: list[float]) -> str:
    """Classify the auction market phase (spec 5.2.5)."""
    if len(candles) < 10:
        return "accumulation"
    recent = candles[-10:]
    closes = np.array([c.close for c in recent])
    rng_pct = (closes.max() - closes.min()) / max(closes.mean(), 1e-9)
    vols = np.array([c.volume for c in recent])
    vol_ratio = vols[-3:].mean() / max(vols.mean(), 1e-9)
    cvd_slope = abs(cvds[-1] - cvds[-10]) if len(cvds) >= 10 else 0.0

    if rng_pct > 0.006 and vol_ratio > 1.2 and cvd_slope > 0:
        return "expansion"
    if rng_pct < 0.0025 and vol_ratio < 1.0:
        return "accumulation"
    if rng_pct < 0.004 and vol_ratio > 1.1:
        return "distribution"
    return "contraction"
