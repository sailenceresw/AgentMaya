"""Core market-data dataclasses shared across all layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Trade:
    """A single aggregated trade tick."""

    timestamp: int          # ms
    price: float
    qty: float
    is_buyer_maker: bool    # True => aggressive SELL; False => aggressive BUY


@dataclass
class Candle:
    """An OHLCV candle plus the trades that built it (for order-flow calc)."""

    timestamp: int          # ms, candle open time
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: list[Trade] = field(default_factory=list)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


Direction = Literal["long", "short"]
