"""Layer 1 — real-time WebSocket ingestion (spec section 4.2).

REST polling (``CCXTFeed``) works but only sees a candle once it's closed
and pieces together its trades with a separate follow-up REST call — not
the continuous aggTrade stream the CVD/absorption indicators are actually
designed around. ``WebSocketFeed`` subscribes directly to Binance's
combined-stream endpoint for ``<symbol>@kline_<tf>`` and
``<symbol>@aggTrade``, accumulating real trade ticks per candle window and
emitting a :class:`~maya.data.models.Candle` the moment Binance marks that
kline closed (``k.x == true``).

This is **opt-in** (``MAYA_USE_WEBSOCKET=true``) and kept alongside
``CCXTFeed`` rather than replacing it — REST polling has fewer failure
modes and is what testnet has actually been exercised against so far.
Prefer REST until this has real testnet runtime behind it.

Bridges asyncio (needed for the ``websockets`` client) to the synchronous
``CandleFeed`` interface the rest of the codebase expects: the event loop
runs on a background thread and candles cross to the caller via a
thread-safe queue. The parsing functions below are pure (no network, no
threading) specifically so the message-handling logic is unit-testable
without a live connection.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Iterator

from loguru import logger

from maya.config import RunMode, Settings
from maya.data.models import Candle, Trade

from maya.timeframe import timeframe_ms

_MAINNET_WS = "wss://fstream.binance.com/stream"
_TESTNET_WS = "wss://stream.binancefuture.com/stream"


def parse_kline_event(msg: dict) -> tuple[Candle | None, bool]:
    """Parse a combined-stream kline event into (candle, is_closed).
    Pure function, no network — directly unit-testable."""
    data = msg.get("data", msg)
    k = data.get("k")
    if k is None:
        return None, False
    candle = Candle(
        timestamp=int(k["t"]), open=float(k["o"]), high=float(k["h"]),
        low=float(k["l"]), close=float(k["c"]), volume=float(k["v"]),
        # 'V' is the taker buy base volume for the bar — the exchange's own
        # aggressive-buy total, which makes CVD exact instead of inferred.
        taker_buy_volume=float(k["V"]) if k.get("V") is not None else None,
    )
    return candle, bool(k.get("x", False))


def parse_agg_trade_event(msg: dict) -> Trade | None:
    """Parse a combined-stream aggTrade event. Pure function, unit-testable."""
    data = msg.get("data", msg)
    if "p" not in data or "q" not in data or "T" not in data:
        return None
    return Trade(
        timestamp=int(data["T"]), price=float(data["p"]), qty=float(data["q"]),
        is_buyer_maker=bool(data["m"]),
    )


class WebSocketFeed:
    """Real-time candle feed backed by Binance USD-M Futures WebSocket
    streams. Implements the same ``candles()`` iterator interface as
    CCXTFeed/SimulatedFeed, so it's a drop-in for ``build_feed()``."""

    def __init__(self, settings: Settings, reconnect_backoff_s: float = 1.0,
                 max_backoff_s: float = 30.0) -> None:
        self.settings = settings
        self.tf_ms = timeframe_ms(settings.timeframe)
        self._max_backoff_s = max_backoff_s
        self._backoff_s = reconnect_backoff_s
        self._queue: "queue.Queue[Candle]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._trades_buffer: dict[int, list[Trade]] = {}

    def _ws_url(self) -> str:
        base = _TESTNET_WS if self.settings.mode is RunMode.TESTNET else _MAINNET_WS
        symbol = self.settings.symbol.lower()
        tf = self.settings.timeframe
        streams = f"{symbol}@kline_{tf}/{symbol}@aggTrade"
        return f"{base}?streams={streams}"

    def _handle_message(self, raw: str) -> Candle | None:
        """Feed one raw WebSocket text frame in; get a completed Candle out,
        or None if this frame was a trade tick / an unfinished kline."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None

        trade = parse_agg_trade_event(msg)
        if trade is not None:
            bucket = (trade.timestamp // self.tf_ms) * self.tf_ms
            self._trades_buffer.setdefault(bucket, []).append(trade)
            return None

        candle, is_closed = parse_kline_event(msg)
        if candle is None or not is_closed:
            return None
        bucket = (candle.timestamp // self.tf_ms) * self.tf_ms
        candle.trades = self._trades_buffer.pop(bucket, [])
        # Drop any older buffered buckets so a long-running connection
        # doesn't leak memory if a kline event is ever missed.
        for stale in [b for b in self._trades_buffer if b < bucket]:
            self._trades_buffer.pop(stale, None)
        return candle

    async def _run_async(self) -> None:
        import websockets  # imported lazily — only needed when opted in

        url = self._ws_url()
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("WebSocket connected: {}", url)
                    self._backoff_s = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        candle = self._handle_message(raw)
                        if candle is not None:
                            self._queue.put(candle)
            except Exception as exc:  # noqa: BLE001 - reconnect, don't crash the thread
                if self._stop.is_set():
                    break
                logger.warning("WebSocket disconnected, reconnecting in {:.1f}s: {}",
                               self._backoff_s, exc)
                await asyncio.sleep(self._backoff_s)
                self._backoff_s = min(self._backoff_s * 2, self._max_backoff_s)

    def _thread_target(self) -> None:
        asyncio.run(self._run_async())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_target,
                                        name="maya-websocket-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def candles(self) -> Iterator[Candle]:
        self.start()
        emitted = 0
        limit = self.settings.max_candles or 10**9
        # A closed kline should arrive roughly every tf_ms; if nothing shows
        # up for several timeframes plus slack, treat the connection as
        # stalled rather than hanging forever (spec 4.6 — circuit breaker on
        # data quality degradation).
        stall_timeout_s = self.tf_ms / 1000 * 3 + 30
        try:
            while emitted < limit:
                try:
                    candle = self._queue.get(timeout=stall_timeout_s)
                except queue.Empty:
                    logger.error("No candle received in {:.0f}s — connection "
                                "likely stalled. Stopping feed.", stall_timeout_s)
                    break
                emitted += 1
                yield candle
        finally:
            self.stop()
