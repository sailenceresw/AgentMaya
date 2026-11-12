"""Layer 5 orchestrator — opens approved trades and manages open positions.

Position management (spec §8.3):
* Single-target setups (TP1 == TP2) close in full at TP1.
* Two-target setups (e.g. value_area_absorption) scale out:
    - TP1 hit  -> close 50%, move stop to breakeven
    - TP2 hit  -> close 25%, start trailing the stop
    - runner   -> remaining 25% trails on 1m structure until stopped / timed out
Breakeven move (pre-TP1) and the max-duration force-close also apply.

For paper/sim the engine simulates fills against each candle's high/low. For
testnet/live the exchange holds the stop + scaled TP orders; this engine still
tracks the position locally and applies duration/trailing management.
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

FEE_RATE = 0.0005      # taker, conservative
TP1_FRACTION = 0.50    # fraction of original size closed at TP1
TP2_FRACTION = 0.25    # fraction closed at TP2 (remaining 25% is the runner)


@dataclass
class TradeResult:
    position: Position
    exit_price: float      # weighted-average exit across tranches
    pnl_usdt: float        # net of fees, whole trade
    fees_usdt: float
    rr_realized: float
    result: str            # win / loss / breakeven
    exit_ts: int
    reason: str            # reason the FINAL tranche closed


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
            initial_stop_loss=intent.stop_loss,
            take_profit_2=intent.take_profit_2,
        )
        return True

    # -- tranche accounting ------------------------------------------------- #
    def _weighted_exit(self, final_price: float, final_qty: float) -> float:
        """Average exit price across realized tranches + the final tranche."""
        pos = self.position
        assert pos is not None
        closed_qty = pos.quantity - pos.remaining_qty
        # Reconstruct average from realized gross: realized_pnl is gross on
        # closed_qty; derive its avg exit, then blend with the final tranche.
        if closed_qty > 0:
            # realized gross = (avg_exit - entry) * closed_qty * dir
            sign = 1 if pos.direction == "long" else -1
            avg_closed_exit = pos.entry_price + sign * (pos.realized_pnl / closed_qty)
        else:
            avg_closed_exit = final_price
        total_qty = closed_qty + final_qty
        if total_qty <= 0:
            return final_price
        return (avg_closed_exit * closed_qty + final_price * final_qty) / total_qty

    def _partial_close(self, qty: float, price: float, reason: str) -> None:
        pos = self.position
        assert pos is not None
        qty = min(qty, pos.remaining_qty)
        gross = pos.pnl_at(price, qty)
        pos.realized_pnl += gross
        pos.remaining_qty -= qty
        self.broker.reduce_position(pos, qty, price, reason)
        logger.info("[{}] scaled out {:.5f} {} @ {:.2f} ({}) gross={:.2f}",
                    self.settings.mode.value.upper(), qty, pos.symbol, price, reason, gross)

    def _finalize(self, exit_price: float, ts: int, reason: str) -> TradeResult:
        pos = self.position
        assert pos is not None
        final_gross = pos.pnl_at(exit_price, pos.remaining_qty)
        total_gross = pos.realized_pnl + final_gross
        fees = pos.notional * FEE_RATE * 2  # entry once + exits summing to notional
        pnl = total_gross - fees
        risk = abs(pos.entry_price - pos.initial_stop_loss) * pos.quantity
        rr = (total_gross / risk) if risk else 0.0
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
        avg_exit = self._weighted_exit(exit_price, pos.remaining_qty)
        self.broker.close_position(pos, exit_price, reason)
        pos.remaining_qty = 0.0
        self.position = None
        return TradeResult(pos, avg_exit, pnl, fees, rr, result, ts, reason)

    # -- per-candle management --------------------------------------------- #
    def manage(self, candle: Candle) -> TradeResult | None:
        pos = self.position
        if pos is None:
            return None
        long = pos.direction == "long"

        def hit(level: float, is_target: bool) -> bool:
            if long:
                return candle.high >= level if is_target else candle.low <= level
            return candle.low <= level if is_target else candle.high >= level

        # 1. Stop-loss on the remaining size (checked before targets on the bar).
        if hit(pos.stop_loss, is_target=False):
            return self._finalize(pos.stop_loss, candle.timestamp, "stop_loss")

        # 2. TP1
        if not pos.tp1_done and hit(pos.take_profit, is_target=True):
            if pos.has_distinct_tp2:
                self._partial_close(TP1_FRACTION * pos.quantity, pos.take_profit, "take_profit_1")
                pos.tp1_done = True
                pos.stop_loss = pos.entry_price           # breakeven on the rest
                pos.moved_to_breakeven = True
                logger.debug("TP1 hit; SL -> breakeven for {}", pos.symbol)
            else:
                return self._finalize(pos.take_profit, candle.timestamp, "take_profit")
        # 3. TP2 (only after TP1, distinct targets)
        elif pos.tp1_done and not pos.tp2_done and pos.has_distinct_tp2 \
                and hit(pos.take_profit_2, is_target=True):
            self._partial_close(TP2_FRACTION * pos.quantity, pos.take_profit_2, "take_profit_2")
            pos.tp2_done = True
            logger.debug("TP2 hit; trailing runner for {}", pos.symbol)

        # 4. Pre-TP1 breakeven move after +0.3% favorable close (spec 8.3).
        if not pos.tp1_done:
            fav = (candle.close - pos.entry_price) / pos.entry_price
            if not long:
                fav = -fav
            if not pos.moved_to_breakeven and fav >= 0.003:
                pos.stop_loss = pos.entry_price
                pos.moved_to_breakeven = True

        # 5. Trail the runner on 1m structure after TP2 (favorable direction only).
        if pos.tp2_done:
            if long:
                pos.stop_loss = max(pos.stop_loss, candle.low)
            else:
                pos.stop_loss = min(pos.stop_loss, candle.high)

        # 6. Max-duration force-close (spec 7.1 / 8.3).
        dur_h = (candle.timestamp - pos.opened_ts) / 3_600_000
        if dur_h >= RiskLimits.MAX_POSITION_DURATION_HOURS:
            return self._finalize(candle.close, candle.timestamp, "max_duration")
        return None

    def force_close(self, price: float, ts: int) -> TradeResult | None:
        if self.position is None:
            return None
        return self._finalize(price, ts, "forced")


def build_execution_engine(settings: Settings):
    """Simulated engine for sim/paper; live exchange-tracking engine for
    testnet/live (real orders must not be simulated)."""
    if settings.uses_real_orders:
        from maya.execution.live_engine import LiveExecutionEngine
        return LiveExecutionEngine(settings)
    return ExecutionEngine(settings)
