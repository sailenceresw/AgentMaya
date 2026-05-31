"""Position sizing (spec section 7.2)."""
from __future__ import annotations

from maya.config import RiskLimits


def calculate_position_notional(account_equity: float, risk_pct: float,
                                entry: float, sl: float) -> float:
    """Notional position size that risks ``risk_pct`` of equity to the stop.

    Capped at ``POSITION_NOTIONAL_CAP_MULT`` x equity (spec 7.1).
    """
    risk_usdt = account_equity * (risk_pct / 100.0)
    sl_distance_pct = abs(entry - sl) / entry if entry else 0.0
    if sl_distance_pct <= 0:
        return 0.0
    notional = risk_usdt / sl_distance_pct
    cap = account_equity * RiskLimits.POSITION_NOTIONAL_CAP_MULT
    return min(notional, cap)


def notional_to_qty(notional: float, price: float) -> float:
    return notional / price if price else 0.0
