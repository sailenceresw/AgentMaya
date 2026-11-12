"""Broker abstractions for the execution engine.

* :class:`PaperBroker` — records orders and reports fills, no network. Used by
  ``sim`` and ``paper`` modes.
* :class:`CCXTBroker`  — places real reduce-only stop / limit orders on Binance
  USD-M Futures (testnet or live) via ccxt.

The execution engine drives position lifecycle; brokers only place/cancel
orders and report fills.
"""
from __future__ import annotations

from typing import Protocol

from loguru import logger

from maya.config import Settings
from maya.execution.models import Position
from maya.strategy.models import TradeIntent


class Broker(Protocol):
    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool: ...
    def close_position(self, pos: Position, price: float, reason: str) -> None: ...
    def reduce_position(self, pos: Position, qty: float, price: float, reason: str) -> None: ...
    def open_state(self) -> dict: ...


class PaperBroker:
    """Simulated broker — assumes limit entries fill at the intent price."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        tp2 = f" TP2={intent.take_profit_2:.2f}" if intent.take_profit_2 else ""
        logger.info("[PAPER] OPEN {} {} qty={:.5f} entry={:.2f} SL={:.2f} TP={:.2f}{}",
                    intent.direction, intent.symbol, qty, intent.entry_price,
                    intent.stop_loss, intent.take_profit_1, tp2)
        return True

    def reduce_position(self, pos: Position, qty: float, price: float, reason: str) -> None:
        # Paper: scaling out is pure local accounting; nothing to send.
        pass

    def open_state(self) -> dict:
        # Paper starts flat each run; nothing on an exchange to reconcile.
        return {"position_qty": 0.0, "open_orders": 0}

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        logger.info("[PAPER] CLOSE {} {} rem={:.5f} @ {:.2f} ({})",
                    pos.direction, pos.symbol, pos.remaining_qty, price, reason)


class CCXTBroker:
    """Live/testnet broker. Places entry (limit), SL (stop-market, reduce-only)
    and TP (limit, reduce-only) orders on the exchange (spec section 8.2).

    Uses ccxt unified symbols and the exchange's own precision filters so order
    amounts/prices satisfy Binance's symbol filters (spec section 8.5)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        from maya.data.binance_client import build_exchange, to_ccxt_symbol

        self.symbol = to_ccxt_symbol(settings.symbol)
        self.ex = build_exchange(settings)
        self.ex.load_markets()

    def _amt(self, qty: float) -> float:
        return float(self.ex.amount_to_precision(self.symbol, qty))

    def _price(self, px: float) -> float:
        return float(self.ex.price_to_precision(self.symbol, px))

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        sym = self.symbol
        side = "buy" if intent.direction == "long" else "sell"
        close_side = "sell" if intent.direction == "long" else "buy"
        try:
            qty = self._amt(qty)
            # Isolated margin + leverage (spec hard limits 7.1).
            try:
                self.ex.set_margin_mode("ISOLATED", sym)
            except Exception as exc:  # noqa: BLE001 - already-set is non-fatal
                logger.debug("set_margin_mode: {}", exc)
            self.ex.set_leverage(leverage, sym)

            self.ex.create_order(sym, "limit", side, qty, self._price(intent.entry_price),
                                 {"timeInForce": "GTC"})
            # Stop-loss held AT the exchange, triggered on MARK price (spec 8.2.2).
            # With closePosition=True the amount MUST be omitted — Binance rejects
            # a quantity alongside closePosition (-1106), which would leave the
            # position with no stop.
            self.ex.create_order(sym, "STOP_MARKET", close_side, None, None, {
                "stopPrice": self._price(intent.stop_loss),
                "closePosition": True, "workingType": "MARK_PRICE",
            })
            # Take-profit orders. Scale out when a distinct TP2 exists: 50% at
            # TP1, 25% at TP2, 25% runner (closed by the stop). Otherwise one
            # reduce-only TP for the full size.
            distinct_tp2 = intent.take_profit_2 > 0 and (
                (intent.direction == "long" and intent.take_profit_2 > intent.take_profit_1)
                or (intent.direction == "short" and intent.take_profit_2 < intent.take_profit_1))
            if distinct_tp2:
                self.ex.create_order(sym, "limit", close_side, self._amt(qty * 0.5),
                                     self._price(intent.take_profit_1), {"reduceOnly": True})
                self.ex.create_order(sym, "limit", close_side, self._amt(qty * 0.25),
                                     self._price(intent.take_profit_2), {"reduceOnly": True})
            else:
                self.ex.create_order(sym, "limit", close_side, qty,
                                     self._price(intent.take_profit_1), {"reduceOnly": True})
            logger.info("[{}] orders placed for {} {} qty={} (scaled={})",
                        self.settings.mode.value.upper(), intent.direction, sym, qty, distinct_tp2)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("order placement failed: {}", exc)
            return False

    def reduce_position(self, pos: Position, qty: float, price: float, reason: str) -> None:
        # Live: scaled TP exits were pre-placed as reduce-only limit orders at
        # open, so the exchange fills them; nothing to send here.
        logger.debug("[{}] tranche {} filled on exchange ({})",
                     self.settings.mode.value.upper(), reason, self.symbol)

    def open_state(self) -> dict:
        """Snapshot the exchange's current position/orders for reconciliation."""
        try:
            positions = self.ex.fetch_positions([self.symbol])
            qty = sum(abs(float(p.get("contracts") or 0)) for p in positions)
            orders = self.ex.fetch_open_orders(self.symbol)
            return {"position_qty": qty, "open_orders": len(orders)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile fetch failed: {}", exc)
            return {"position_qty": 0.0, "open_orders": 0, "error": str(exc)}

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        side = "sell" if pos.direction == "long" else "buy"
        try:
            # Cancel any resting TP/SL orders, then flatten the remainder.
            try:
                self.ex.cancel_all_orders(self.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug("cancel_all_orders: {}", exc)
            self.ex.create_order(self.symbol, "market", side,
                                 self._amt(max(pos.remaining_qty, 0) or pos.quantity),
                                 None, {"reduceOnly": True})
            logger.info("[{}] market close {} ({})",
                        self.settings.mode.value.upper(), self.symbol, reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("close failed: {}", exc)


def build_broker(settings: Settings) -> Broker:
    if settings.uses_real_orders:
        from maya.config import Venue
        if settings.venue is Venue.HYPERLIQUID:
            from maya.execution.hyperliquid_broker import HyperliquidBroker
            return HyperliquidBroker(settings)
        return CCXTBroker(settings)
    return PaperBroker(settings)
