"""Layer 5 — Hyperliquid execution broker.

Signs with the agent wallet. Places a limit entry, a reduce-only stop-market
trigger, and scaled take-profit triggers (50% TP1 / 25% TP2 when a distinct TP2
exists, else one full TP). Hyperliquid sizes are in coin units and prices must
respect the asset's precision, so sizes/prices are rounded from the venue meta.
"""
from __future__ import annotations

import math

from loguru import logger

from maya.config import Settings
from maya.execution.models import Position
from maya.strategy.models import TradeIntent


def round_px(px: float, sz_decimals: int) -> float:
    """Round a price to Hyperliquid's rules: <=5 significant figures and at most
    (6 - szDecimals) decimal places for perps."""
    if px <= 0:
        return 0.0
    d = math.floor(math.log10(abs(px)))
    factor = 10 ** (5 - 1 - d)
    px5 = round(px * factor) / factor
    return round(px5, max(0, 6 - sz_decimals))


class HyperliquidBroker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.coin = settings.coin
        self.address = settings.hyperliquid_account_address
        from maya.data.hyperliquid_client import build_exchange, build_info
        self.ex = build_exchange(settings)
        self.info = build_info(settings)
        self.sz_decimals = self._load_sz_decimals()

    def _load_sz_decimals(self) -> int:
        try:
            for a in self.info.meta()["universe"]:
                if a["name"] == self.coin:
                    return int(a["szDecimals"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("meta load failed, assuming szDecimals=3: {}", exc)
        return 3

    def _sz(self, qty: float) -> float:
        return round(qty, self.sz_decimals)

    def _px(self, px: float) -> float:
        return round_px(px, self.sz_decimals)

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        coin = self.coin
        is_buy = intent.direction == "long"
        close_is_buy = not is_buy
        sz = self._sz(qty)
        try:
            self.ex.update_leverage(leverage, coin, is_cross=False)  # isolated margin
            # Limit entry (GTC).
            self.ex.order(coin, is_buy, sz, self._px(intent.entry_price),
                          {"limit": {"tif": "Gtc"}}, reduce_only=False)
            # Stop-loss: reduce-only market trigger.
            sl_px = self._px(intent.stop_loss)
            self.ex.order(coin, close_is_buy, sz, sl_px,
                          {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
                          reduce_only=True)
            # Take-profit(s): scale out on a distinct TP2, else one full TP.
            distinct_tp2 = intent.take_profit_2 > 0 and (
                (is_buy and intent.take_profit_2 > intent.take_profit_1)
                or (not is_buy and intent.take_profit_2 < intent.take_profit_1))
            if distinct_tp2:
                self._tp(close_is_buy, self._sz(qty * 0.5), intent.take_profit_1)
                self._tp(close_is_buy, self._sz(qty * 0.25), intent.take_profit_2)
            else:
                self._tp(close_is_buy, sz, intent.take_profit_1)
            logger.info("[HL {}] {} {} sz={} entry={} SL={} (scaled={})",
                        self.settings.mode.value.upper(), intent.direction, coin,
                        sz, self._px(intent.entry_price), sl_px, distinct_tp2)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Hyperliquid order placement failed: {}", exc)
            return False

    def _tp(self, close_is_buy: bool, sz: float, px: float) -> None:
        tp_px = self._px(px)
        self.ex.order(self.coin, close_is_buy, sz, tp_px,
                      {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
                      reduce_only=True)

    def reduce_position(self, pos: Position, qty: float, price: float, reason: str) -> None:
        # Scaled TP triggers were pre-placed; the exchange fills them.
        logger.debug("[HL] tranche {} filled on exchange ({})", reason, self.coin)

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        try:
            self.ex.market_close(self.coin)
            logger.info("[HL {}] market close {} ({})",
                        self.settings.mode.value.upper(), self.coin, reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("Hyperliquid close failed: {}", exc)

    def open_state(self) -> dict:
        """Snapshot current position/orders for reconciliation (spec §8.4)."""
        try:
            addr = self.address
            qty = 0.0
            if addr:
                state = self.info.user_state(addr)
                for ap in state.get("assetPositions", []):
                    pos = ap.get("position", {})
                    if pos.get("coin") == self.coin:
                        qty = abs(float(pos.get("szi") or 0))
                orders = self.info.open_orders(addr)
                open_orders = sum(1 for o in orders if o.get("coin") == self.coin)
            else:
                open_orders = 0
            return {"position_qty": qty, "open_orders": open_orders}
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile fetch failed: {}", exc)
            return {"position_qty": 0.0, "open_orders": 0, "error": str(exc)}
