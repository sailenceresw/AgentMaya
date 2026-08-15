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
from maya.risk.sizing import (
    calculate_margin_notional,
    calculate_position_notional,
    margin_required,
    notional_to_qty,
)
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


_DAY_MS = 86_400_000
_WEEK_MS = 7 * _DAY_MS


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
    # Period boundaries the counters above are scoped to. -1 = not yet anchored;
    # the first roll_periods() call anchors them without resetting anything, so
    # counters restored from the journal survive startup. Not 0 -- that is a
    # legitimate epoch day, and tests/backtests do start the clock there.
    day_start_ts: int = -1
    week_start_ts: int = -1


class RiskManager:
    def __init__(self, settings: Settings, equity: float) -> None:
        self.settings = settings
        self.account = AccountState(
            equity=equity, start_equity=equity,
            day_start_equity=equity, week_start_equity=equity,
        )

    @classmethod
    def from_journal(cls, settings: Settings, equity: float, journal, now_ts: int) -> "RiskManager":
        """Build a RiskManager whose session counters are restored from the
        trade journal (spec section 8.4 state reconciliation) instead of
        always starting at zero. This matters because ``trades_today``,
        ``consecutive_losses`` and the daily-loss-limit calc are hard risk
        floors — silently resetting them on every restart would let a
        process crash/restart quietly bypass the day's trade cap or the
        cooldown after consecutive losses.

        Restoration is always *additively conservative*, matching the same
        philosophy as the LLM veto layer: on ambiguity it can only make the
        system more restrictive, never less.
        """
        rm = cls(settings, equity)
        acc = rm.account
        try:
            state = journal.session_state(now_ts)
        except Exception as exc:  # noqa: BLE001 - never let journal issues loosen risk
            logger.error("could not read journal session state ({}); "
                         "assuming a fresh session (trades_today=0)", exc)
            return rm

        acc.trades_today = state["trades_today"]
        acc.consecutive_losses = state["consecutive_losses"]
        # day_start_equity is derived, not stored: today's realized PnL is
        # already reflected in `equity`, so back it out to get where the day
        # actually started, which is what the daily-loss kill switch needs.
        acc.day_start_equity = equity - state["pnl_today_usdt"]

        if acc.consecutive_losses >= RiskLimits.MAX_CONSECUTIVE_LOSSES:
            acc.state = SystemState.COOLDOWN
            acc.cooldown_until_ts = now_ts + RiskLimits.COOLDOWN_MINUTES * 60_000
            logger.warning(
                "Restored {} consecutive losses from journal — starting in "
                "COOLDOWN for {}min rather than assuming a clean slate.",
                acc.consecutive_losses, RiskLimits.COOLDOWN_MINUTES,
            )

        if acc.trades_today > 0 or acc.consecutive_losses > 0:
            logger.info(
                "Risk state restored from journal: trades_today={} "
                "consecutive_losses={} day_start_equity=${:.2f}",
                acc.trades_today, acc.consecutive_losses, acc.day_start_equity,
            )
        return rm

    # -- kill switch ------------------------------------------------------- #
    # Halts that a new period clears, mapped to the period that clears them.
    # A "daily" loss limit that stops the system forever is not a daily limit;
    # it is a one-shot kill switch wearing the wrong name. Everything not
    # listed here (the equity floor, a stale feed, a manual halt) is sticky:
    # those describe conditions that a calendar rollover does not repair.
    _DAILY_HALT = "daily loss limit"
    _WEEKLY_HALT = "weekly loss limit"

    def _halt(self, reason: str) -> None:
        if self.account.state is not SystemState.HALTED:
            logger.critical("KILL SWITCH — HALT: {}", reason)
        self.account.state = SystemState.HALTED
        if reason not in self.account.halt_reasons:
            self.account.halt_reasons.append(reason)

    def _clear_halts(self, prefix: str) -> None:
        """Drop halts of one kind; resume only if nothing else is holding."""
        remaining = [r for r in self.account.halt_reasons if not r.startswith(prefix)]
        if len(remaining) == len(self.account.halt_reasons):
            return
        self.account.halt_reasons = remaining
        if not remaining and self.account.state is SystemState.HALTED:
            self.account.state = SystemState.RUNNING
            logger.info("New period — {} halt cleared, trading resumes", prefix)

    def roll_periods(self, now_ts: int) -> None:
        """Roll the daily/weekly counters when the clock crosses a boundary.

        Without this ``trades_today`` only ever increments and
        ``day_start_equity`` never moves, which silently turns three periodic
        limits into lifetime ones: MAX_TRADES_PER_DAY becomes a cap of 5 trades
        for the life of the process, and the "daily"/"weekly" loss limits
        measure drawdown from session start forever rather than from today's
        open.

        A new period also clears the halt raised *by that period's* loss limit
        — and only that one. A daily-loss halt that never lifts is a permanent
        kill switch, not a daily limit. Halts describing conditions a calendar
        change does not repair (equity floor, stale data, manual) stay put, and
        a period rollover can never clear more than one kind at a time, so a
        system halted for two reasons stays halted.
        """
        acc = self.account
        day = (now_ts // _DAY_MS) * _DAY_MS
        week = (now_ts // _WEEK_MS) * _WEEK_MS

        if acc.day_start_ts < 0:  # first observation — anchor, don't reset
            acc.day_start_ts, acc.week_start_ts = day, week
            return

        if day > acc.day_start_ts:
            acc.day_start_ts = day
            acc.trades_today = 0
            acc.day_start_equity = acc.equity
            self._clear_halts(self._DAILY_HALT)
        if week > acc.week_start_ts:
            acc.week_start_ts = week
            acc.week_start_equity = acc.equity
            self._clear_halts(self._WEEKLY_HALT)

    def check_kill_switch(self, now_ts: int, data_stale: bool = False) -> None:
        """Evaluate kill-switch triggers (spec section 7.3)."""
        self.roll_periods(now_ts)
        acc = self.account
        daily_dd = (acc.day_start_equity - acc.equity) / acc.day_start_equity * 100
        weekly_dd = (acc.week_start_equity - acc.equity) / acc.week_start_equity * 100

        if daily_dd >= RiskLimits.DAILY_LOSS_LIMIT_PCT:
            self._halt(f"{self._DAILY_HALT} hit ({daily_dd:.2f}%)")
        if weekly_dd >= RiskLimits.WEEKLY_LOSS_LIMIT_PCT:
            self._halt(f"{self._WEEKLY_HALT} hit ({weekly_dd:.2f}%)")
        if acc.equity < RiskLimits.EQUITY_FLOOR_USDT:
            self._halt(f"equity below floor (${acc.equity:.2f})")
        if data_stale:
            self._halt("data feed interrupted")

    def manual_halt(self) -> None:
        self._halt("manual halt command")

    # -- per-intent screening ---------------------------------------------- #
    def screen(self, intent: TradeIntent, now_ts: int) -> RiskDecision:
        # Idempotent, and screen() is also called directly (tests, tooling), so
        # the caps are always evaluated against the correct period.
        self.roll_periods(now_ts)
        acc = self.account

        if acc.state is SystemState.HALTED:
            return RiskDecision(False, "system halted")
        if acc.state is SystemState.COOLDOWN and now_ts < acc.cooldown_until_ts:
            return RiskDecision(False, "in cooldown")
        if acc.state is SystemState.COOLDOWN:
            acc.state = SystemState.RUNNING  # cooldown elapsed

        if acc.open_positions >= RiskLimits.MAX_CONCURRENT_POSITIONS:
            return RiskDecision(False, "max concurrent positions")
        if acc.trades_today >= RiskLimits.MAX_TRADES_PER_DAY:
            return RiskDecision(False, "max trades per day")
        leverage = self.settings.leverage
        # ROI is screened on *margin*, not on raw price movement. Comparing an
        # unleveraged price move against a percentage target rejected every
        # realistic 1m scalp — a 40% BTC move never happens intraday.
        roi = intent.roi_on_margin_pct(leverage)
        if roi < self.settings.min_roi_pct:
            return RiskDecision(
                False,
                f"ROI {roi:.1f}% on margin ({intent.expected_roi_pct:.2f}% move "
                f"x {leverage}x) < {self.settings.min_roi_pct:.1f}%")
        if intent.expected_rr < RiskLimits.MIN_RR_FOR_ENTRY:
            return RiskDecision(False, f"R:R {intent.expected_rr:.1f} < {RiskLimits.MIN_RR_FOR_ENTRY}")

        if self.settings.sizing_mode == "margin":
            notional = calculate_margin_notional(
                acc.equity, self.settings.margin_per_trade_usd, leverage,
                intent.entry_price, intent.stop_loss,
            )
        else:
            notional = calculate_position_notional(
                acc.equity, self.settings.risk_per_trade_usd,
                intent.entry_price, intent.stop_loss,
            )
        if notional <= 0:
            return RiskDecision(False, "invalid position size")

        # Never commit more collateral than the account holds.
        margin = margin_required(notional, leverage)
        if margin > acc.equity:
            return RiskDecision(
                False, f"margin ${margin:.2f} exceeds equity ${acc.equity:.2f}")

        qty = notional_to_qty(notional, intent.entry_price)
        return RiskDecision(
            approved=True, reason="ok",
            position_notional=notional, quantity=qty, leverage=leverage,
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
