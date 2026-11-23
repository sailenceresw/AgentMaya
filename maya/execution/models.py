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
    # Best price reached since entry (highest high for a long, lowest low for
    # a short). The trailing stop follows this once the profit lock has fired.
    best_price: float | None = None
    # Set once an R-based trail has started (profit_trail_mode='r').
    trail_armed: bool = False
    # The venue's own entry fee, when the broker read it from the fill
    # history. None means unknown: the fee model is charged instead.
    entry_fee_usd: float | None = None

    def __post_init__(self) -> None:
        if self.initial_stop_loss is None:
            self.initial_stop_loss = self.stop_loss
        if self.best_price is None:
            self.best_price = self.entry_price

    def is_tighter(self, new_stop: float) -> bool:
        """True when ``new_stop`` is closer to profit than the current stop.

        This is the only test a stop move has to pass, and it is one-way: a
        stop may rise under a long or fall over a short, never the reverse.
        """
        if self.direction == "long":
            return new_stop > self.stop_loss
        return new_stop < self.stop_loss

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
