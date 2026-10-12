"""Broker abstractions for the execution engine.

* :class:`PaperBroker` — records orders and reports fills, no network. Used by
  ``sim`` and ``paper`` modes.
* :class:`CCXTBroker`  — places real reduce-only stop / limit orders on Binance
  USD-M Futures (testnet or live) via ccxt. Used when ``venue=binance``.
* :class:`~maya.execution.metamask_broker.MetaMaskPerpsBroker` — places
  Hyperliquid perp orders through the MetaMask Agent Wallet ``mm`` CLI. Used
  when ``venue=hyperliquid``. Self-custodial, so it needs no exchange API
  keys; it cannot trade Binance, and the ccxt brokers cannot trade
  Hyperliquid, so the two venues are mutually exclusive per run.

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
    def update_stop(self, pos: Position, new_stop: float) -> bool: ...


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

    def update_stop(self, pos: Position, new_stop: float) -> bool:
        # Nothing to sync — there's no exchange-side order in paper mode.
        # The caller already updates pos.stop_loss locally.
        return True


class CCXTBroker:
    """Live/testnet broker. Places entry (limit), SL (stop-market, reduce-only)
    and TP (limit, reduce-only) orders on the exchange (spec section 8.2)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        import ccxt

        ex = ccxt.binanceusdm({
            "apiKey": settings.binance_api_key,
            "secret": settings.binance_api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if settings.mode.value == "testnet":
            ex.set_sandbox_mode(True)
        self.ex = ex

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        sym = intent.symbol
        side = "buy" if intent.direction == "long" else "sell"
        close_side = "sell" if intent.direction == "long" else "buy"
        try:
            self.ex.set_margin_mode("isolated", sym)
            self.ex.set_leverage(leverage, sym)
            self.ex.create_order(sym, "limit", side, qty, intent.entry_price,
                                 {"timeInForce": "GTC"})
            # Stop-loss at the exchange (workingType MARK_PRICE) — spec 8.2.2.
            self.ex.create_order(sym, "STOP_MARKET", close_side, None, None,
                                 _close_position_stop(intent.stop_loss))
            self.ex.create_order(sym, "limit", close_side, qty, intent.take_profit_1,
                                 {"reduceOnly": True})
            logger.info("[{}] orders placed for {} {}",
                        self.settings.mode.value.upper(), intent.direction, sym)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("order placement failed: {}", exc)
            return False

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        side = "sell" if pos.direction == "long" else "buy"
        try:
            self.ex.create_order(pos.symbol, "market", side, pos.quantity,
                                 None, {"reduceOnly": True})
            logger.info("[{}] market close {} ({})",
                        self.settings.mode.value.upper(), pos.symbol, reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("close failed: {}", exc)

    def update_stop(self, pos: Position, new_stop: float) -> bool:
        """Cancel the existing STOP_MARKET reduce-only order and replace it
        at ``new_stop`` (used for the breakeven move — spec 8.3). Without
        this, moving ``pos.stop_loss`` locally does nothing on the actual
        exchange and the original stop stays live."""
        close_side = "sell" if pos.direction == "long" else "buy"
        try:
            open_orders = self.ex.fetch_open_orders(pos.symbol)
            for o in open_orders:
                if o.get("type", "").upper() in ("STOP_MARKET", "STOP") and \
                        o.get("side") == close_side:
                    self.ex.cancel_order(o["id"], pos.symbol)
            self.ex.create_order(pos.symbol, "STOP_MARKET", close_side, None, None,
                                 _close_position_stop(new_stop))
            logger.info("[{}] stop moved to {:.2f} for {}",
                        self.settings.mode.value.upper(), new_stop, pos.symbol)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("stop update failed — original stop may still be live "
                        "at the old price: {}", exc)
            return False


def _close_position_stop(stop_price: float) -> dict:
    """Params for a stop that closes the whole position at the mark price.

    Binance rejects ``closePosition=true`` sent together with ``reduceOnly``
    or a quantity (-1106 "parameter sent when not required"). Sending
    ``reduceOnly`` here made every stop-loss placement fail, which left each
    position with no exchange-side stop. ``closePosition`` is already
    reduce-only by definition, so nothing is lost by omitting it.
    """
    return {"stopPrice": stop_price, "closePosition": True, "workingType": "MARK_PRICE"}


def build_broker(settings: Settings) -> Broker:
    if not settings.uses_real_orders:
        return PaperBroker(settings)
    if settings.uses_metamask:
        # Imported lazily so sim/paper runs never need the `mm` CLI present,
        # mirroring how ccxt is only imported when actually used.
        from maya.execution.metamask_broker import MetaMaskPerpsBroker

        return MetaMaskPerpsBroker(settings)
    return CCXTBroker(settings)
