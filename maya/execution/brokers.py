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


class PaperBroker:
    """Simulated broker — assumes limit entries fill at the intent price."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        logger.info("[PAPER] OPEN {} {} qty={:.5f} entry={:.2f} SL={:.2f} TP={:.2f}",
                    intent.direction, intent.symbol, qty, intent.entry_price,
                    intent.stop_loss, intent.take_profit_1)
        return True

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        logger.info("[PAPER] CLOSE {} {} @ {:.2f} ({}) pnl={:.2f}",
                    pos.direction, pos.symbol, price, reason, pos.pnl_at(price))


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
            self.ex.create_order(sym, "STOP_MARKET", close_side, qty, None, {
                "stopPrice": self._price(intent.stop_loss),
                "closePosition": True, "workingType": "MARK_PRICE",
            })
            self.ex.create_order(sym, "limit", close_side, qty,
                                 self._price(intent.take_profit_1), {"reduceOnly": True})
            logger.info("[{}] orders placed for {} {} qty={}",
                        self.settings.mode.value.upper(), intent.direction, sym, qty)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("order placement failed: {}", exc)
            return False

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        side = "sell" if pos.direction == "long" else "buy"
        try:
            self.ex.create_order(self.symbol, "market", side, self._amt(pos.quantity),
                                 None, {"reduceOnly": True})
            logger.info("[{}] market close {} ({})",
                        self.settings.mode.value.upper(), self.symbol, reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("close failed: {}", exc)


def build_broker(settings: Settings) -> Broker:
    if settings.uses_real_orders:
        return CCXTBroker(settings)
    return PaperBroker(settings)
