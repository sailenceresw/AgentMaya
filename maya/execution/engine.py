"""Layer 5 orchestrator — opens approved trades and manages open positions.

For paper/sim modes the engine simulates SL/TP fills against each candle's
high/low and applies the position-management rules from spec section 8.3
(breakeven move, max duration force-close). For testnet/live the exchange holds
the stop/TP orders; this engine still tracks the position locally and applies
duration-based force closes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from loguru import logger

from maya.config import BINANCE_FEE_RATE, RiskLimits, Settings
from maya.data.models import Candle
from maya.execution.brokers import build_broker
from maya.execution.models import Position
from maya.risk.manager import RiskDecision
from maya.strategy.models import TradeIntent

# Default per-side fee rate (Binance USD-M taker, conservative). The engine
# reads the venue-resolved rate from Settings; this stays as the module-level
# default other code and tests reference.
FEE_RATE = BINANCE_FEE_RATE


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
        self.fee_rate = settings.effective_fee_rate
        self.broker = build_broker(settings)
        self.position: Position | None = None
        # Guards `position` against a race between the candle-driven graph
        # thread (manage/open) and the background PositionMonitor thread
        # (testnet/live), which can both observe/close a position.
        self.lock = threading.RLock()

    @property
    def has_position(self) -> bool:
        return self.position is not None

    def open(self, intent: TradeIntent, decision: RiskDecision, ts: int) -> bool:
        with self.lock:
            if self.has_position:
                return False
            ok = self.broker.open_position(intent, decision.quantity, decision.leverage)
            if not ok:
                return False
            # A broker that can read the venue's fill (MetaMask/Hyperliquid)
            # reports it, so PnL, R and fees are computed from the price and
            # size actually traded — a market entry's slippage or a partially
            # filled limit would otherwise be invisible in the journal.
            entry, qty, notional = intent.entry_price, decision.quantity, decision.position_notional
            fill = getattr(self.broker, "last_fill", None)
            if fill is not None and fill.price > 0 and fill.size > 0:
                entry, qty, notional = fill.price, fill.size, fill.price * fill.size
            self.position = Position(
                symbol=intent.symbol, direction=intent.direction,
                setup_name=intent.setup_name, entry_price=entry,
                stop_loss=intent.stop_loss, take_profit=intent.take_profit_1,
                quantity=qty, notional=notional,
                leverage=decision.leverage, opened_ts=ts,
                confluence_score=intent.confluence_score, rr_planned=intent.expected_rr,
                confluences=list(intent.confluences),
            )
            return True

    def _close(self, exit_price: float, ts: int, reason: str) -> TradeResult:
        pos = self.position
        assert pos is not None
        gross = pos.pnl_at(exit_price)
        fees = pos.notional * self.fee_rate * 2  # entry + exit
        pnl = gross - fees
        risk = abs(pos.entry_price - pos.stop_loss) * pos.quantity
        rr = (gross / risk) if risk else 0.0
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
        self.broker.close_position(pos, exit_price, reason)
        self.position = None
        return TradeResult(pos, exit_price, pnl, fees, rr, result, ts, reason)

    def manage(self, candle: Candle) -> TradeResult | None:
        """Advance the open position against a new candle. Returns a result on close.

        For testnet/live, a :class:`~maya.execution.monitor.PositionMonitor`
        polls the exchange faster than the 1-minute candle cadence and is
        the authoritative source for real SL/TP fills — see
        :meth:`reconcile_external_close`. This candle-driven check is what
        actually executes fills in sim/paper mode, and in testnet/live it's
        a slower-cadence fallback plus the only path for breakeven moves and
        max-duration force-closes (which the exchange doesn't do on its
        own). If the monitor already closed the position, ``self.position``
        will be ``None`` here and this is a harmless no-op.
        """
        with self.lock:
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

            # Move stop to breakeven after +0.3% favorable close — applies to
            # the NEXT bar onward, never the bar that triggered it (spec 8.3).
            fav = (candle.close - pos.entry_price) / pos.entry_price
            if pos.direction == "short":
                fav = -fav
            if not pos.moved_to_breakeven and fav >= 0.003:
                new_stop = pos.entry_price
                if self.broker.update_stop(pos, new_stop):
                    pos.stop_loss = new_stop
                    pos.moved_to_breakeven = True
                    logger.debug("SL moved to breakeven for {}", pos.symbol)

            # Max duration force-close (spec 7.1 / 8.3).
            dur_h = (candle.timestamp - pos.opened_ts) / 3_600_000
            if dur_h >= RiskLimits.MAX_POSITION_DURATION_HOURS:
                return self._close(candle.close, candle.timestamp, "max_duration")
            return None

    def force_close(self, price: float, ts: int) -> TradeResult | None:
        with self.lock:
            if self.position is None:
                return None
            return self._close(price, ts, "forced")

    def reconcile_external_close(self, exit_price: float, ts: int, reason: str,
                                  fees_usdt: float | None = None) -> TradeResult | None:
        """Close the local position to match a fill that already happened on
        the exchange (detected by :class:`~maya.execution.monitor.PositionMonitor`).

        Unlike :meth:`_close`, this does **not** call ``broker.close_position``
        — the exchange-side SL/TP order already executed the close; sending
        another reduce-only order here would be redundant at best and, if
        the exchange state is even slightly stale, a wrong-side order at
        worst. Fees can be overridden with the actual fill fee data.
        """
        with self.lock:
            pos = self.position
            if pos is None:
                return None
            gross = pos.pnl_at(exit_price)
            fees = fees_usdt if fees_usdt is not None else pos.notional * self.fee_rate * 2
            pnl = gross - fees
            risk = abs(pos.entry_price - pos.stop_loss) * pos.quantity
            rr = (gross / risk) if risk else 0.0
            result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
            self.position = None
            logger.info("[{}] exchange-confirmed close {} {} @ {:.2f} ({})",
                        self.settings.mode.value.upper(), pos.direction, pos.symbol,
                        exit_price, reason)
            return TradeResult(pos, exit_price, pnl, fees, rr, result, ts, reason)
