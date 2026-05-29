"""Trade intent emitted by the strategy engine (spec section 6.5)."""
from __future__ import annotations

from dataclasses import dataclass, field

from maya.data.models import Direction


@dataclass
class TradeIntent:
    symbol: str
    direction: Direction
    setup_name: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    confluence_score: int
    confluences: list[str] = field(default_factory=list)
    expected_rr: float = 0.0
    timestamp: int = 0

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.stop_loss)
