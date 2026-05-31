"""Position model used by the execution engine."""
from __future__ import annotations

from dataclasses import dataclass, field

from maya.data.models import Direction


@dataclass
class Position:
    symbol: str
    direction: Direction
    setup_name: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    notional: float
    leverage: int
    opened_ts: int
    confluence_score: int
    rr_planned: float
    confluences: list[str] = field(default_factory=list)
    moved_to_breakeven: bool = False

    def unrealized_pnl(self, price: float) -> float:
        diff = price - self.entry_price
        if self.direction == "short":
            diff = -diff
        return diff * self.quantity

    def pnl_at(self, exit_price: float) -> float:
        diff = exit_price - self.entry_price
        if self.direction == "short":
            diff = -diff
        return diff * self.quantity
