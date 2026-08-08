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
