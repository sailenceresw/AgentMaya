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
    """An OHLCV candle plus the order flow that built it.

    Order flow can arrive two ways:

    - ``taker_buy_volume`` — the exchange's own aggressive-buy total for the
      candle (Binance kline field 9, ``V`` on the websocket). This is *real*
      order flow and is preferred whenever present.
    - ``trades`` — individual ticks, needed only if something wants intra-candle
      sequencing. Nothing currently does: every order-flow indicator in
      ``maya.signals.indicators`` reads OHLCV, and CVD needs only the
      buy/sell split, which ``taker_buy_volume`` already gives exactly.
    """

    timestamp: int          # ms, candle open time
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: list[Trade] = field(default_factory=list)
    taker_buy_volume: float | None = None

    @property
    def delta(self) -> float | None:
        """Aggressive buy volume minus aggressive sell volume, or None.

        Sells are whatever was not a taker buy, so
        ``delta = buys - (volume - buys) = 2*buys - volume``. Returns None when
        the exchange figure is absent, so callers can fall back to summing
        ticks rather than silently reporting a delta of zero.
        """
        if self.taker_buy_volume is None:
            return None
        return 2.0 * self.taker_buy_volume - self.volume

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
