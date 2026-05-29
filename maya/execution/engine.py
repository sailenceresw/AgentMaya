"""Layer 5 orchestrator — opens approved trades and manages open positions.

For paper/sim modes the engine simulates SL/TP fills against each candle's
high/low and applies the position-management rules from spec section 8.3
(breakeven move, max duration force-close). For testnet/live the exchange holds
the stop/TP orders; this engine still tracks the position locally and applies
duration-based force closes.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from maya.config import RiskLimits, Settings
from maya.data.models import Candle
from maya.execution.brokers import build_broker
from maya.execution.models import Position
from maya.risk.manager import RiskDecision
from maya.strategy.models import TradeIntent

FEE_RATE = 0.0005  # taker, conservative


@dataclass
class TradeResult:
    position: Position
    exit_price: float
    pnl_usdt: float
    fees_usdt: float
    rr_realized: float
    result: str            # win / loss / breakeven
    exit_ts: int
    reason: str


class ExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.broker = build_broker(settings)
        self.position: Position | None = None

    @property
    def has_position(self) -> bool:
        return self.position is not None

    def open(self, intent: TradeIntent, decision: RiskDecision, ts: int) -> bool:
        if self.has_position:
            return False
        ok = self.broker.open_position(intent, decision.quantity, decision.leverage)
        if not ok:
            return False
        self.position = Position(
            symbol=intent.symbol, direction=intent.direction,
            setup_name=intent.setup_name, entry_price=intent.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit_1,
            quantity=decision.quantity, notional=decision.position_notional,
            leverage=decision.leverage, opened_ts=ts,
            confluence_score=intent.confluence_score, rr_planned=intent.expected_rr,
            confluences=list(intent.confluences),
        )
        return True

    def _close(self, exit_price: float, ts: int, reason: str) -> TradeResult:
        pos = self.position
        assert pos is not None
        gross = pos.pnl_at(exit_price)
        fees = pos.notional * FEE_RATE * 2  # entry + exit
        pnl = gross - fees
        risk = abs(pos.entry_price - pos.stop_loss) * pos.quantity
        rr = (gross / risk) if risk else 0.0
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
        self.broker.close_position(pos, exit_price, reason)
        self.position = None
        return TradeResult(pos, exit_price, pnl, fees, rr, result, ts, reason)

    def manage(self, candle: Candle) -> TradeResult | None:
        """Advance the open position against a new candle. Returns a result on close."""
        pos = self.position
        if pos is None:
            return None

        # Check SL / TP against the stop as it stands at candle open
        # (conservative: stop checked before target on the same bar).
        if pos.direction == "long":
            if candle.low <= pos.stop_loss:
                return self._close(pos.stop_loss, candle.timestamp, "stop_loss")
            if candle.high >= pos.take_profit:
                return self._close(pos.take_profit, candle.timestamp, "take_profit")
        else:
            if candle.high >= pos.stop_loss:
                return self._close(pos.stop_loss, candle.timestamp, "stop_loss")
            if candle.low <= pos.take_profit:
                return self._close(pos.take_profit, candle.timestamp, "take_profit")

        # Move stop to breakeven after +0.3% favorable close — applies to the
        # NEXT bar onward, never the bar that triggered it (spec 8.3).
        fav = (candle.close - pos.entry_price) / pos.entry_price
        if pos.direction == "short":
            fav = -fav
        if not pos.moved_to_breakeven and fav >= 0.003:
            pos.stop_loss = pos.entry_price
            pos.moved_to_breakeven = True
            logger.debug("SL moved to breakeven for {}", pos.symbol)

        # Max duration force-close (spec 7.1 / 8.3).
        dur_h = (candle.timestamp - pos.opened_ts) / 3_600_000
        if dur_h >= RiskLimits.MAX_POSITION_DURATION_HOURS:
            return self._close(candle.close, candle.timestamp, "max_duration")
        return None

    def force_close(self, price: float, ts: int) -> TradeResult | None:
        if self.position is None:
            return None
        return self._close(price, ts, "forced")
