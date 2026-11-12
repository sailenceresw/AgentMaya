"""Position model used by the execution engine.

Supports scaling out in tranches (TP1, TP2, runner). ``quantity`` is the
original size; ``remaining_qty`` shrinks as tranches close, and ``realized_pnl``
/ ``realized_fees`` accumulate the closed tranches so the final result reflects
the whole trade.
"""
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
    take_profit: float          # TP1 (first target)
    quantity: float             # original size
    notional: float
    leverage: int
    opened_ts: int
    confluence_score: int
    rr_planned: float
    confluences: list[str] = field(default_factory=list)
    moved_to_breakeven: bool = False
    initial_stop_loss: float = 0.0   # original stop, for true R-multiple accounting
    take_profit_2: float = 0.0       # TP2 (second target); 0 => single-target trade
    remaining_qty: float = 0.0       # unclosed size (set to quantity at open)
    realized_pnl: float = 0.0        # PnL from tranches already closed
    realized_fees: float = 0.0       # fees from tranches already closed
    tp1_done: bool = False
    tp2_done: bool = False

    def __post_init__(self) -> None:
        if self.remaining_qty == 0.0 and not self.tp1_done:
            self.remaining_qty = self.quantity

    @property
    def has_distinct_tp2(self) -> bool:
        """True when TP2 is a genuine further target (enables scaling out)."""
        if self.take_profit_2 <= 0:
            return False
        if self.direction == "long":
            return self.take_profit_2 > self.take_profit
        return self.take_profit_2 < self.take_profit

    def unrealized_pnl(self, price: float) -> float:
        diff = price - self.entry_price
        if self.direction == "short":
            diff = -diff
        return diff * self.remaining_qty

    def pnl_at(self, exit_price: float, qty: float | None = None) -> float:
        q = self.remaining_qty if qty is None else qty
        diff = exit_price - self.entry_price
        if self.direction == "short":
            diff = -diff
        return diff * q
