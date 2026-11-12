"""Layer 5 — LIVE execution engine (testnet / mainnet).

Unlike :class:`~maya.execution.engine.ExecutionEngine` (which simulates fills
against candles for sim/paper), this engine trades the *real* exchange and
tracks the *real* position:

* On open it places the entry + exchange-side stop and scaled take-profit
  orders, then treats the position as PENDING until the exchange confirms a
  fill (or cancels it after a timeout with no fill).
* Exits are handled by the exchange's own stop/TP trigger orders. Each candle
  the engine polls the real position; when it goes flat, the trade is finalized
  using realized PnL from the exchange fills.
* Only max-duration force-closes and the final flatten are initiated locally.

This keeps local state in lockstep with the exchange and never assumes a fill
that did not happen — essential before real money.
"""
from __future__ import annotations

from loguru import logger

from maya.config import RiskLimits, Settings
from maya.data.models import Candle
from maya.execution.brokers import build_broker
from maya.execution.engine import TradeResult
from maya.execution.models import Position
from maya.risk.manager import RiskDecision
from maya.strategy.models import TradeIntent

FILL_TIMEOUT_CANDLES = 3   # cancel an unfilled entry after this many candles
_EPS = 1e-9


class LiveExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.broker = build_broker(settings)
        self.position: Position | None = None
        self._pending = False
        self._pending_candles = 0

    @property
    def has_position(self) -> bool:
        return self.position is not None

    def open(self, intent: TradeIntent, decision: RiskDecision, ts: int) -> bool:
        if self.has_position:
            return False
        if not self.broker.open_position(intent, decision.quantity, decision.leverage):
            return False
        self.position = Position(
            symbol=intent.symbol, direction=intent.direction,
            setup_name=intent.setup_name, entry_price=intent.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit_1,
            quantity=decision.quantity, notional=decision.position_notional,
            leverage=decision.leverage, opened_ts=ts,
            confluence_score=intent.confluence_score, rr_planned=intent.expected_rr,
            confluences=list(intent.confluences),
            initial_stop_loss=intent.stop_loss, take_profit_2=intent.take_profit_2,
        )
        self._pending = True
        self._pending_candles = 0
        logger.info("[LIVE] entry submitted, awaiting fill: {} {}",
                    intent.direction, intent.symbol)
        return True

    def _finalize_from_exchange(self, ts: int, reason: str) -> TradeResult:
        pos = self.position
        assert pos is not None
        pnl = self.broker.realized_since(pos.opened_ts)
        self.broker.cancel_resting()   # clean up any leftover trigger orders
        risk = abs(pos.entry_price - pos.initial_stop_loss) * pos.quantity
        rr = (pnl / risk) if risk else 0.0
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
        logger.info("[LIVE] position closed on exchange ({}) realized PnL={:.2f}", reason, pnl)
        self.position = None
        self._pending = False
        return TradeResult(pos, pos.entry_price, pnl, 0.0, rr, result, ts, reason)

    def manage(self, candle: Candle) -> TradeResult | None:
        pos = self.position
        if pos is None:
            return None
        qty = self.broker.sync_qty()

        if self._pending:
            if abs(qty) > _EPS:
                self._pending = False
                logger.info("[LIVE] entry filled ({:+.5f})", qty)
            else:
                self._pending_candles += 1
                if self._pending_candles >= FILL_TIMEOUT_CANDLES:
                    self.broker.cancel_resting()
                    logger.info("[LIVE] entry unfilled after {} candles — cancelled",
                                FILL_TIMEOUT_CANDLES)
                    self.position = None
                return None

        # Confirmed open. The exchange's stop/TP triggers manage exits.
        if abs(qty) <= _EPS:
            return self._finalize_from_exchange(candle.timestamp, "exchange")

        # Max-duration force-close (spec §7.1 / §8.3).
        dur_h = (candle.timestamp - pos.opened_ts) / 3_600_000
        if dur_h >= RiskLimits.MAX_POSITION_DURATION_HOURS:
            self.broker.close_position(pos, candle.close, "max_duration")
            return self._finalize_from_exchange(candle.timestamp, "max_duration")
        return None

    def force_close(self, price: float, ts: int) -> TradeResult | None:
        if self.position is None:
            return None
        self.broker.close_position(self.position, price, "forced")
        return self._finalize_from_exchange(ts, "forced")
