"""Real order flow from Hyperliquid's websocket ``trades`` stream.

Subscribes to per-trade data (price, size, aggressor side) and buffers it so the
candle feed can attach the *real* trades to each closed candle, giving true CVD
instead of the kline-close approximation.

Robust by design: if the websocket can't be reached the feed simply falls back
to the approximation. The SDK runs the socket in its own thread; we only guard
the shared buffer with a lock.
"""
from __future__ import annotations

import threading
from collections import deque

from loguru import logger

from maya.config import Settings
from maya.data.models import Trade


class HyperliquidTradeAggregator:
    def __init__(self, settings: Settings, maxlen: int = 50_000) -> None:
        self.settings = settings
        self.coin = settings.coin
        self._lock = threading.Lock()
        self._buf: deque[tuple[int, float, float, bool]] = deque(maxlen=maxlen)
        self._info = None
        self._sub_id = None
        self.active = False

    def start(self) -> bool:
        try:
            from hyperliquid.info import Info
            from maya.data.hyperliquid_client import base_url
            self._info = Info(base_url(self.settings), skip_ws=False)
            self._sub_id = self._info.subscribe(
                {"type": "trades", "coin": self.coin}, self._on_msg)
            self.active = True
            logger.info("Hyperliquid trades websocket subscribed ({})", self.coin)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS trades unavailable, using approximated CVD: {}", exc)
            self.active = False
            return False

    def _on_msg(self, msg) -> None:
        data = (msg or {}).get("data") or []
        with self._lock:
            for t in data:
                if t.get("coin") not in (self.coin, None):
                    continue
                try:
                    ts = int(t["time"])
                    px = float(t["px"])
                    sz = float(t["sz"])
                except (KeyError, ValueError, TypeError):
                    continue
                # HL side: "A" = aggressive sell (buyer is maker), "B" = agg buy.
                is_buyer_maker = t.get("side") == "A"
                self._buf.append((ts, px, sz, is_buyer_maker))

    def trades_for(self, start_ms: int, end_ms: int) -> list[Trade]:
        with self._lock:
            return [Trade(ts, px, sz, bm) for (ts, px, sz, bm) in self._buf
                    if start_ms <= ts < end_ms]

    def stop(self) -> None:
        try:
            if self._info is not None and self._sub_id is not None:
                self._info.unsubscribe({"type": "trades", "coin": self.coin}, self._sub_id)
            if self._info is not None:
                self._info.disconnect_websocket()
        except Exception:  # noqa: BLE001
            pass
        self.active = False
