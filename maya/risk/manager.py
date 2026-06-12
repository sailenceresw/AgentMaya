"""Layer 4 — Risk manager with hard limits, kill switch and position sizing.

This layer has VETO POWER over everything (spec section 7). The strategy engine
cannot bypass it. Every trade intent is screened here before it can become an
order, and the kill switch can HALT the whole system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from maya.config import RiskLimits, Settings
from maya.risk.sizing import calculate_position_notional, notional_to_qty
from maya.strategy.models import TradeIntent


class SystemState(str, Enum):
    RUNNING = "running"
    HALTED = "halted"          # no new trades; manage existing
    COOLDOWN = "cooldown"      # temporary pause after consecutive losses


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_notional: float = 0.0
    quantity: float = 0.0
    leverage: int = RiskLimits.DEFAULT_LEVERAGE


@dataclass
class AccountState:
    equity: float
    start_equity: float
    day_start_equity: float
    week_start_equity: float
    open_positions: int = 0
    trades_today: int = 0
    consecutive_losses: int = 0
    state: SystemState = SystemState.RUNNING
    cooldown_until_ts: int = 0
    halt_reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, settings: Settings, equity: float) -> None:
        self.settings = settings
        self.account = AccountState(
            equity=equity, start_equity=equity,
            day_start_equity=equity, week_start_equity=equity,
        )
        self._day: int | None = None
        self._week: int | None = None

    # -- period rollover --------------------------------------------------- #
    def roll_periods(self, now_ts: int) -> None:
        """Reset daily/weekly counters and baselines when the clock rolls over.

        Without this the daily trade count and loss baselines would never reset,
        so the system would lock up after the first day. A daily/cooldown HALT is
        lifted on a new day; ``check_kill_switch`` (run immediately after) will
        re-halt at once if a weekly limit or the equity floor is still breached.
        """
        day = now_ts // 86_400_000
        week = day // 7
        if self._day is None:
            self._day, self._week = day, week
            return

        if day != self._day:
            self._day = day
            self.account.trades_today = 0
            self.account.day_start_equity = self.account.equity
            self.account.consecutive_losses = 0
            # Lift a daily-loss / data HALT on the new day. Leave an active
            # COOLDOWN to expire on its own timer (it may run past midnight).
            if self.account.state is SystemState.HALTED:
                self.account.state = SystemState.RUNNING
                self.account.halt_reasons = []
        if week != self._week:
            self._week = week
            self.account.week_start_equity = self.account.equity

    # -- kill switch ------------------------------------------------------- #
    def _halt(self, reason: str) -> None:
        if self.account.state is not SystemState.HALTED:
            logger.critical("KILL SWITCH — HALT: {}", reason)
        self.account.state = SystemState.HALTED
        if reason not in self.account.halt_reasons:
            self.account.halt_reasons.append(reason)

    def check_kill_switch(self, now_ts: int, data_stale: bool = False) -> None:
        """Evaluate kill-switch triggers (spec section 7.3)."""
        acc = self.account
        daily_dd = (acc.day_start_equity - acc.equity) / acc.day_start_equity * 100
        weekly_dd = (acc.week_start_equity - acc.equity) / acc.week_start_equity * 100

        if daily_dd >= RiskLimits.DAILY_LOSS_LIMIT_PCT:
            self._halt(f"daily loss limit hit ({daily_dd:.2f}%)")
        if weekly_dd >= RiskLimits.WEEKLY_LOSS_LIMIT_PCT:
            self._halt(f"weekly loss limit hit ({weekly_dd:.2f}%)")
        if acc.equity < RiskLimits.EQUITY_FLOOR_USDT:
            self._halt(f"equity below floor (${acc.equity:.2f})")
        if data_stale:
            self._halt("data feed interrupted")

    def manual_halt(self) -> None:
        self._halt("manual halt command")

    # -- per-intent screening ---------------------------------------------- #
    def screen(self, intent: TradeIntent, now_ts: int) -> RiskDecision:
        acc = self.account

        if acc.state is SystemState.HALTED:
            return RiskDecision(False, "system halted")
        if acc.state is SystemState.COOLDOWN and now_ts < acc.cooldown_until_ts:
            return RiskDecision(False, "in cooldown")
        if acc.state is SystemState.COOLDOWN:
            # Cooldown served — resume AND clear the streak, otherwise the next
            # single loss re-trips the limit and we loop cooldown forever.
            acc.state = SystemState.RUNNING
            acc.consecutive_losses = 0

        if acc.open_positions >= RiskLimits.MAX_CONCURRENT_POSITIONS:
            return RiskDecision(False, "max concurrent positions")
        if acc.trades_today >= RiskLimits.MAX_TRADES_PER_DAY:
            return RiskDecision(False, "max trades per day")
        if intent.expected_rr < RiskLimits.MIN_RR_FOR_ENTRY:
            return RiskDecision(False, f"R:R {intent.expected_rr:.1f} < {RiskLimits.MIN_RR_FOR_ENTRY}")

        notional = calculate_position_notional(
            acc.equity, RiskLimits.MAX_RISK_PER_TRADE_PCT,
            intent.entry_price, intent.stop_loss,
        )
        if notional <= 0:
            return RiskDecision(False, "invalid position size")
        qty = notional_to_qty(notional, intent.entry_price)

        return RiskDecision(
            approved=True, reason="ok",
            position_notional=notional, quantity=qty,
            leverage=RiskLimits.DEFAULT_LEVERAGE,
        )

    # -- post-trade accounting --------------------------------------------- #
    def on_trade_opened(self) -> None:
        self.account.open_positions += 1
        self.account.trades_today += 1

    def on_trade_closed(self, pnl_usdt: float, now_ts: int) -> None:
        acc = self.account
        acc.open_positions = max(0, acc.open_positions - 1)
        acc.equity += pnl_usdt
        if pnl_usdt < 0:
            acc.consecutive_losses += 1
            if acc.consecutive_losses >= RiskLimits.MAX_CONSECUTIVE_LOSSES:
                acc.state = SystemState.COOLDOWN
                acc.cooldown_until_ts = now_ts + RiskLimits.COOLDOWN_MINUTES * 60_000
                logger.warning("{} consecutive losses — {}min cooldown",
                               acc.consecutive_losses, RiskLimits.COOLDOWN_MINUTES)
        else:
            acc.consecutive_losses = 0
        self.check_kill_switch(now_ts)
