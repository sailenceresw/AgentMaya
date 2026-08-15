"""Sentinel — the risk voice.

Sentinel already holds an absolute veto as the L4 risk node. What it gains here
is a *voice*: instead of silently rejecting an intent after the fact, it states
up front when the account cannot take another trade and why.

This is reporting only. Nothing in this module loosens a limit or decides
anything — ``RiskManager.screen`` remains the sole authority, and it runs after
Maya regardless of what Sentinel said in conference. If the two ever disagree,
``screen`` wins.
"""
from __future__ import annotations

from maya.agents.models import BLOCK, NEUTRAL, Finding
from maya.config import RiskLimits
from maya.risk.manager import RiskManager, SystemState

AGENT = "Sentinel"


def observe(risk: RiskManager, now_ts: int) -> Finding:
    """Can the account take a trade right now?"""
    acc = risk.account
    # Roll first so the answer reflects the current period rather than a stale
    # counter — the same rollover screen() will apply moments later.
    risk.roll_periods(now_ts)

    data = {
        "state": acc.state.value,
        "trades_today": acc.trades_today,
        "max_trades_per_day": RiskLimits.MAX_TRADES_PER_DAY,
        "consecutive_losses": acc.consecutive_losses,
        "equity": round(acc.equity, 2),
        "open_positions": acc.open_positions,
    }

    if acc.state is SystemState.HALTED:
        return Finding(AGENT, BLOCK, 1.0,
                       f"system halted: {'; '.join(acc.halt_reasons) or 'unknown'}",
                       data)

    if acc.state is SystemState.COOLDOWN and now_ts < acc.cooldown_until_ts:
        mins = (acc.cooldown_until_ts - now_ts) / 60_000
        return Finding(AGENT, BLOCK, 1.0,
                       f"cooldown after {acc.consecutive_losses} consecutive "
                       f"losses — {mins:.0f}min remaining", data)

    if acc.open_positions >= RiskLimits.MAX_CONCURRENT_POSITIONS:
        return Finding(AGENT, BLOCK, 1.0, "position already open", data)

    if acc.trades_today >= RiskLimits.MAX_TRADES_PER_DAY:
        return Finding(AGENT, BLOCK, 1.0,
                       f"daily trade cap reached ({acc.trades_today}/"
                       f"{RiskLimits.MAX_TRADES_PER_DAY})", data)

    if acc.equity < RiskLimits.EQUITY_FLOOR_USDT:
        return Finding(AGENT, BLOCK, 1.0,
                       f"equity ${acc.equity:.2f} below floor", data)

    remaining = RiskLimits.MAX_TRADES_PER_DAY - acc.trades_today
    daily_dd = ((acc.day_start_equity - acc.equity) / acc.day_start_equity * 100
                if acc.day_start_equity else 0.0)
    return Finding(AGENT, NEUTRAL, 0.0,
                   f"clear — {remaining} trade(s) left today, "
                   f"day P&L {-daily_dd:+.2f}%",
                   data | {"trades_remaining": remaining,
                           "daily_drawdown_pct": round(daily_dd, 2)})
