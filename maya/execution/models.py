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
    # The stop as placed at entry. ``stop_loss`` moves (breakeven), but the
    # trade's risk — the denominator of its R-multiple — was fixed at entry.
    initial_stop_loss: float | None = None

    def __post_init__(self) -> None:
        if self.initial_stop_loss is None:
            self.initial_stop_loss = self.stop_loss

    @property
    def initial_risk_usd(self) -> float:
        return abs(self.entry_price - self.initial_stop_loss) * self.quantity

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
