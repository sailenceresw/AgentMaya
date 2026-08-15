"""Signal object emitted by Layer 2 on each candle close (spec section 5.3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VolumeProfile:
    vah: float
    poc: float
    val: float
    shape: str  # D / P / b / trend


@dataclass
class Absorption:
    detected: bool = False
    side: Optional[str] = None      # 'bullish' | 'bearish'
    level: Optional[float] = None
    strength: float = 0.0           # 0..1


@dataclass
class HTFContext:
    """What one higher-timeframe chart says, read mechanically.

    ``trend`` (EMA-derived) and ``structure`` (swing-derived) are kept separate
    on purpose: they answer different questions and their disagreement is
    itself a signal. See :mod:`maya.signals.technicals`.
    """

    timeframe: str                        # '15m', '1h', ...
    trend: str = "unknown"                # up / down / neutral / unknown
    structure: str = "unknown"            # higher_highs / lower_lows / range
    price: float = 0.0                    # last close on this timeframe
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    support: Optional[float] = None       # nearest confirmed swing low below
    resistance: Optional[float] = None    # nearest confirmed swing high above
    candles: int = 0                      # closed candles available


@dataclass
class SignalObject:
    timestamp: int
    symbol: str
    price: float           # candle close
    high: float = 0.0
    low: float = 0.0
    cvd_1m: float = 0.0
    cvd_session: float = 0.0
    cvd_divergence: Optional[str] = None  # bullish/bearish/hidden_*
    volume_profile: Optional[VolumeProfile] = None
    absorption: Absorption = field(default_factory=Absorption)
    amt_phase: str = "unknown"            # accumulation/expansion/distribution/contraction
    funding_rate: float = 0.0
    long_short_ratio: float = 1.0
    volume: float = 0.0
    avg_volume: float = 0.0
    range_pct: float = 0.0                # rolling range as % of price
    # Average True Range of the entry timeframe. Stops are placed off this
    # rather than off a fixed percentage: a 0.3% stop is ~4x BTC's typical 1m
    # ATR, which made the reward:risk of a value-area fade structurally
    # unreachable regardless of how good the entry was.
    atr: Optional[float] = None
    # Candlestick patterns completing on this candle, and their net read.
    patterns: list[str] = field(default_factory=list)
    pattern_bias: str = "neutral"
    # Higher-timeframe chart reading, keyed by timeframe label ('15m', '1h').
    # Empty until enough 1m candles have been folded to close one HTF bar.
    htf: dict[str, HTFContext] = field(default_factory=dict)
