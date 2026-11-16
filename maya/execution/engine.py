"""Layer 5 orchestrator — opens approved trades and manages open positions.

For paper/sim modes the engine simulates SL/TP fills against each candle's
high/low and applies the position-management rules from spec section 8.3
(profit lock, trailing stop, max duration force-close). For testnet/live the exchange holds
the stop/TP orders; this engine still tracks the position locally and applies
duration-based force closes.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field

from loguru import logger

from maya.config import BINANCE_FEE_RATE, RiskLimits, Settings
from maya.data.models import Candle
from maya.execution.brokers import build_broker
from maya.execution.models import Position
from maya.fees import FeeModel
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


@dataclass
class PendingOrder:
    """A simulated post-only entry resting at ``intent.entry_price``."""

    intent: TradeIntent
    decision: RiskDecision
    placed_ts: int
    outcome: "_Outcome"
    waited: int = 0


@dataclass
class _Outcome:
    """Tracks where price went after an order was placed, filled or not."""

    filled: bool
    direction: str
    level: float
    candles_left: int


@dataclass
class MakerStats:
    placed: int = 0
    filled: int = 0
    missed: int = 0
    # Signed return from the order's level to the close ``horizon`` candles
    # after it was placed, in the order's direction.
    filled_forward: list[float] = field(default_factory=list)
    missed_forward: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        def mean_bps(xs: list[float]) -> float | None:
            return round(sum(xs) / len(xs) * 10_000, 2) if xs else None
        f, m = mean_bps(self.filled_forward), mean_bps(self.missed_forward)
        return {
            "placed": self.placed, "filled": self.filled, "missed": self.missed,
            "missed_share": round(self.missed / self.placed, 4) if self.placed else None,
            "filled_forward_bps": f, "missed_forward_bps": m,
            # Positive means the orders that did not fill went on to do better
            # than the ones that did: the fills were the adverse ones.
            "adverse_selection_bps": round(m - f, 2) if f is not None and m is not None else None,
        }


def flatten_on_shutdown(execution: "ExecutionEngine", risk, journal, price: float,
                        ts: int) -> TradeResult | None:
    """Close any open position at ``price``/``ts`` and account for it fully.

    Every runner used to call ``force_close(entry_price, 0)`` here: the trade
    was booked at its own entry price (≈$0 PnL whatever the market had done),
    stamped 1 January 1970, and never reported to the risk manager. In a
    backtest that also planted a 1970 point in the equity curve. Callers pass
    the last price they actually saw.
    """
    if not execution.has_position:
        return None
    result = execution.force_close(price, ts)
    if result is not None:
        journal.record(result, result.position.confluences)
        risk.on_trade_closed(result.pnl_usdt, ts)
    return result


class ExecutionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fee_rate = settings.effective_fee_rate
        self.fees = FeeModel.for_settings(settings)
        # Entries on Binance are always limits; on Hyperliquid it is configured.
        self.order_type = settings.perps_order_type if settings.uses_metamask or \
            settings.venue.value == "hyperliquid" else "limit"
        self.broker = build_broker(settings)
        self.position: Position | None = None
        # Post-only simulation (paper path only; see Settings.paper_entry_mode).
        self.post_only = (settings.paper_entry_mode == "post_only"
                          and not settings.uses_real_orders)
        self.pending: PendingOrder | None = None
        self.maker = MakerStats()
        self._outcomes: list[_Outcome] = []
        self._fill_rng = random.Random(settings.maker_seed)
        self._filled_this_candle = False
        # Guards `position` against a race between the candle-driven graph
        # thread (manage/open) and the background PositionMonitor thread
        # (testnet/live), which can both observe/close a position.
        self.lock = threading.RLock()

    @property
    def has_position(self) -> bool:
        return self.position is not None

    @property
    def busy(self) -> bool:
        """A position is open or an entry order is resting. Either way the
        strategy must not propose another trade."""
        return self.position is not None or self.pending is not None

    def pop_fill(self) -> bool:
        """True once after a resting order filled, so the caller can tell the
        risk manager a trade opened. The fill happens inside :meth:`manage`,
        not :meth:`open`, which is why it needs its own signal."""
        filled, self._filled_this_candle = self._filled_this_candle, False
        return filled

    def open(self, intent: TradeIntent, decision: RiskDecision, ts: int) -> bool:
        with self.lock:
            if self.busy:
                return False
            if self.post_only:
                # Rest the order; manage() decides on later candles whether it
                # filled. Returning False keeps the caller from counting a
                # trade that has not happened yet.
                outcome = _Outcome(False, intent.direction, intent.entry_price,
                                   self.settings.maker_adverse_horizon_candles)
                self.pending = PendingOrder(intent, decision, ts, outcome)
                self.maker.placed += 1
                self._outcomes.append(outcome)
                logger.debug("post-only {} {} resting at {:.2f}", intent.direction,
                             intent.symbol, intent.entry_price)
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
        fees = self.fees.cost_usd(pos.notional, self.order_type, reason)
        pnl = gross - fees
        # Against the stop placed at entry: after a breakeven move the current
        # stop sits at entry, the risk reads as zero and every such trade —
        # winners included — was journaled as 0R.
        risk = pos.initial_risk_usd
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
            if self.post_only:
                self._mark_outcomes(candle)
                if self.pending is not None:
                    # A fill opens the position at the end of this candle; its
                    # stop and target are first checked against the next one.
                    self._work_pending(candle)
                    return None
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

            self._protect_profit(pos, candle)

            # Max duration force-close (spec 7.1 / 8.3).
            dur_h = (candle.timestamp - pos.opened_ts) / 3_600_000
            if dur_h >= RiskLimits.MAX_POSITION_DURATION_HOURS:
                return self._close(candle.close, candle.timestamp, "max_duration")
            return None

    def _work_pending(self, candle: Candle) -> None:
        """Fill, keep waiting on, or cancel the resting entry order.

        A buy at ``level`` can only have filled if someone sold below it, so
        the candle must trade *through* the level, not just touch it. Even
        then the order may have been too far back in the queue, which is what
        the fill probability stands for.
        """
        order = self.pending
        assert order is not None
        intent, level = order.intent, order.intent.entry_price
        through = candle.low < level if intent.direction == "long" else candle.high > level
        if through and self._fill_rng.random() < self.settings.maker_fill_probability:
            d = order.decision
            self.broker.open_position(intent, d.quantity, d.leverage)
            self.position = Position(
                symbol=intent.symbol, direction=intent.direction,
                setup_name=intent.setup_name, entry_price=level,
                stop_loss=intent.stop_loss, take_profit=intent.take_profit_1,
                quantity=d.quantity, notional=d.position_notional, leverage=d.leverage,
                opened_ts=candle.timestamp, confluence_score=intent.confluence_score,
                rr_planned=intent.expected_rr, confluences=list(intent.confluences))
            self.pending = None
            self.maker.filled += 1
            self._filled_this_candle = True
            order.outcome.filled = True
            return
        order.waited += 1
        if order.waited >= self.settings.maker_order_ttl_candles:
            self.pending = None
            self.maker.missed += 1
            logger.debug("post-only {} {} expired unfilled", intent.direction, intent.symbol)

    def _mark_outcomes(self, candle: Candle) -> None:
        still: list[_Outcome] = []
        for o in self._outcomes:
            o.candles_left -= 1
            if o.candles_left > 0:
                still.append(o)
                continue
            fwd = (candle.close - o.level) / o.level
            if o.direction == "short":
                fwd = -fwd
            (self.maker.filled_forward if o.filled else self.maker.missed_forward).append(fwd)
        self._outcomes = still

    def _protect_profit(self, pos: Position, candle: Candle) -> None:
        """Profit lock, then trailing stop. Takes effect from the NEXT bar,
        never the bar that triggered it (the stop/target check above already
        ran against the old stop).

        1. Once a close is ``profit_lock_trigger_pct`` in favour, the stop
           moves to entry plus the round-trip fees, so a reversal from there
           closes flat after costs rather than at a small loss.
        2. From then on the stop trails ``profit_trail_pct`` behind the best
           price seen.

        Every move goes through :meth:`_tighten_stop`, which refuses anything
        that is not closer to profit than the stop already in place.
        """
        long = pos.direction == "long"
        pos.best_price = max(pos.best_price, candle.high) if long \
            else min(pos.best_price, candle.low)

        if not pos.moved_to_breakeven:
            fav = (candle.close - pos.entry_price) / pos.entry_price
            if not long:
                fav = -fav
            if fav < self.settings.profit_lock_trigger_pct / 100.0:
                return
            fee_offset = pos.entry_price * self.fees.worst_round_trip(self.order_type)
            lock = pos.entry_price + fee_offset if long else pos.entry_price - fee_offset
            if self._tighten_stop(pos, lock):
                pos.moved_to_breakeven = True
                logger.debug("SL locked at breakeven plus fees for {}", pos.symbol)
            if not pos.moved_to_breakeven:
                return

        trail = self.settings.profit_trail_pct / 100.0
        target = pos.best_price * (1 - trail) if long else pos.best_price * (1 + trail)
        self._tighten_stop(pos, target)

    def _tighten_stop(self, pos: Position, new_stop: float) -> bool:
        """Move the stop to ``new_stop`` if, and only if, that is closer to
        profit. There is deliberately no method that widens or removes a stop."""
        if not pos.is_tighter(new_stop):
            return False
        if not self.broker.update_stop(pos, new_stop):
            return False
        pos.stop_loss = new_stop
        return True

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
            fees = fees_usdt if fees_usdt is not None else \
                self.fees.cost_usd(pos.notional, self.order_type, reason)
            pnl = gross - fees
            risk = pos.initial_risk_usd
            rr = (gross / risk) if risk else 0.0
            result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
            self.position = None
            logger.info("[{}] exchange-confirmed close {} {} @ {:.2f} ({})",
                        self.settings.mode.value.upper(), pos.direction, pos.symbol,
                        exit_price, reason)
            return TradeResult(pos, exit_price, pnl, fees, rr, result, ts, reason)
