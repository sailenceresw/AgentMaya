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

    @property
    def expected_roi_pct(self) -> float:
        """Expected move to TP1 as a percentage of price — **unleveraged**.

        Note this is a *price move*, not a return on your money. For a 1m BTC
        scalp it is a low single-digit number, so screening it against a
        percentage target only makes sense via :meth:`roi_on_margin_pct`.
        """
        if not self.entry_price:
            return 0.0
        return abs(self.take_profit_1 - self.entry_price) / abs(self.entry_price) * 100.0

    def roi_on_margin_pct(self, leverage: int) -> float:
        """Expected return on the margin posted, i.e. the price move geared by
        leverage. This is what a "minimum ROI" threshold actually means: at
        20x, a 4.6% move to target is a 92.8% return on the collateral at
        risk, not a 4.6% one."""
        return self.expected_roi_pct * max(1, leverage)
