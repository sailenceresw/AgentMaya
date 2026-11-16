"""Position sizing (spec section 7.2).

Two sizing conventions are supported, and they are not interchangeable:

* **risk-based** — choose the loss you are willing to take; the stop distance
  then determines the position size. Leverage affects only how much margin is
  locked, never the P&L.
* **margin-based** — choose the collateral you commit; leverage then
  determines the position size, and the loss on a stop-out is whatever the
  stop distance implies. This is what "using $20 at 20x" normally means.

Both are clamped by the same hard limits: the notional cap
(``POSITION_NOTIONAL_CAP_MULT`` x equity), what the account's margin can
actually support (``equity x leverage``), and — importantly for margin-based
sizing — ``MAX_RISK_PER_TRADE_USD``, so that an unusually wide stop cannot
turn a small margin commitment into an oversized loss.
"""
from __future__ import annotations

from maya.config import RiskLimits


def stop_distance_frac(entry: float, sl: float) -> float:
    """Stop distance as a fraction of entry price. 0.0 if undefined."""
    if not entry:
        return 0.0
    return abs(entry - sl) / entry


def max_risk_usd(account_equity: float) -> float:
    """The most a single stop-out may lose: the smaller of the fixed USD
    ceiling and ``MAX_RISK_PER_TRADE_PCT`` of equity. Every sizing mode is
    clamped by this, so no configuration can risk more."""
    return max(0.0, min(RiskLimits.MAX_RISK_PER_TRADE_USD,
                        account_equity * RiskLimits.MAX_RISK_PER_TRADE_PCT / 100.0))


def calculate_position_notional(account_equity: float, risk_amount: float,
                                entry: float, sl: float) -> float:
    """Notional position size that risks a fixed USD amount to the stop.

    The amount is clamped by :func:`max_risk_usd` and the notional by
    ``POSITION_NOTIONAL_CAP_MULT`` x equity.
    """
    sl_distance_pct = stop_distance_frac(entry, sl)
    if sl_distance_pct <= 0 or risk_amount <= 0:
        return 0.0
    notional = min(risk_amount, max_risk_usd(account_equity)) / sl_distance_pct
    cap = account_equity * RiskLimits.POSITION_NOTIONAL_CAP_MULT
    return min(notional, cap)


def calculate_pct_notional(account_equity: float, risk_pct: float,
                           entry: float, sl: float) -> float:
    """Notional that risks ``risk_pct`` percent of equity to the stop:
    ``(equity x risk_pct) / stop_distance_pct``, then the same clamps as
    :func:`calculate_position_notional`."""
    return calculate_position_notional(
        account_equity, account_equity * risk_pct / 100.0, entry, sl)


def calculate_margin_notional(account_equity: float, margin_amount: float,
                              leverage: int, entry: float, sl: float) -> float:
    """Notional from a fixed margin commitment at ``leverage``.

    ``margin_amount x leverage``, then clamped so that none of the hard limits
    is breached:

    1. the notional cap (``POSITION_NOTIONAL_CAP_MULT`` x equity),
    2. what the account could margin at all (``equity x leverage``),
    3. :func:`max_risk_usd` — the stop-out loss implied by this notional
       must not exceed the per-trade risk ceiling. Without this a wide stop
       would quietly turn a "$20 position" into a much larger loss, which is
       the main hazard of sizing by margin rather than by risk.
    """
    sl_distance_pct = stop_distance_frac(entry, sl)
    if sl_distance_pct <= 0 or leverage < 1 or margin_amount <= 0:
        return 0.0

    notional = margin_amount * leverage
    notional = min(notional,
                   account_equity * RiskLimits.POSITION_NOTIONAL_CAP_MULT,
                   account_equity * leverage,
                   max_risk_usd(account_equity) / sl_distance_pct)
    return max(0.0, notional)


def notional_to_qty(notional: float, price: float) -> float:
    return notional / price if price else 0.0


def margin_required(notional: float, leverage: int) -> float:
    return notional / leverage if leverage else 0.0


def liquidation_distance_frac(leverage: int, maintenance_margin: float) -> float:
    """Approximate adverse move, as a fraction of entry, that liquidates an
    isolated position: the initial margin (1/leverage) less the maintenance
    margin the venue keeps back. Ignores fees and funding, which only bring
    liquidation closer — callers apply a buffer for that."""
    return max(1.0 / max(leverage, 1) - maintenance_margin, 0.0)
