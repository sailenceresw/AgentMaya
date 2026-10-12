"""Layer 1 — Data ingestion.

Provides candle feeds for each run mode:

* :class:`SimulatedFeed`  — synthetic random-walk data with embedded order flow.
                            No network, no keys. Used by ``sim`` mode.
* :class:`CCXTFeed`       — real Binance USD-M Futures klines + agg trades via
                            ccxt. Used by ``paper``/``testnet``/``live`` modes.

Both expose ``candles()`` — an iterator yielding :class:`~maya.data.models.Candle`.
The candle carries the underlying trades so Layer 2 can compute CVD/absorption.
"""
from __future__ import annotations

import random
import time
from typing import Iterator, Protocol

from loguru import logger

from maya.config import Settings
from maya.data.models import Candle, Trade
from maya.data.scheduler import CandleScheduler

from maya.timeframe import timeframe_ms


class CandleFeed(Protocol):
    def candles(self) -> Iterator[Candle]:  # pragma: no cover - interface
        ...


# --------------------------------------------------------------------------- #
# Simulated feed
# --------------------------------------------------------------------------- #
class SimulatedFeed:
    """Generates synthetic candles with a regime-switching random walk.

    The walk deliberately cycles through quiet ranges, expansions and sweeps so
    that the strategy setups have a chance to trigger during a short demo run.
    """

    BASE_VOL = 120.0

    def __init__(self, settings: Settings, seed: int | None = 42) -> None:
        self.settings = settings
        self.rng = random.Random(seed)
        self.price = 67_500.0
        self.tf_ms = timeframe_ms(settings.timeframe)
        self.t = int(time.time() * 1000) - (settings.candle_limit or 500) * self.tf_ms
        self.recent_high = self.price
        self.recent_low = self.price
        self._absorb_left = 0          # remaining candles in an absorption burst
        self._absorb_side = "bullish"  # 'bullish' (lower wicks) / 'bearish' (upper)
        self._absorb_level = self.price

    def _regime(self, i: int) -> tuple[float, float]:
        """Return (drift_pct, vol_pct) for candle index ``i`` as fractions."""
        phase = (i // 25) % 4
        # accumulation, expansion-up, distribution, expansion-down.
        # Expansion phases trend hard so liquidity pools sit far away, which is
        # what makes the strategy's demanding 1:4-1:5 R:R achievable.
        return [
            (0.0, 0.0008),      # tight range  (~0.08%)
            (0.0022, 0.0020),   # trending up  (~0.22%/candle drift)
            (0.0, 0.0015),      # choppy distribution
            (-0.0022, 0.0020),  # trending down
        ][phase]

    def _make_trades(self, ts: int, o: float, c: float, vol: float,
                     bias: float | None = None) -> list[Trade]:
        n = self.rng.randint(8, 20)
        if bias is None:
            bias = 0.62 if c >= o else 0.38  # P(aggressive buy)
        trades: list[Trade] = []
        for k in range(n):
            px = o + (c - o) * (k / max(n - 1, 1)) + self.rng.gauss(0, abs(c - o) / 5 + 1)
            qty = abs(self.rng.gauss(vol / n, vol / n / 2)) + 1e-6
            is_buyer_maker = self.rng.random() > bias  # maker == sell aggressor
            trades.append(Trade(timestamp=ts, price=px, qty=qty, is_buyer_maker=is_buyer_maker))
        return trades

    def candles(self) -> Iterator[Candle]:
        n = self.settings.candle_limit or 500
        for i in range(n):
            drift, vol_pct = self._regime(i)
            o = self.price
            ret = self.rng.gauss(drift, vol_pct) * o
            c = max(1.0, o + ret)
            wick = abs(self.rng.gauss(0, vol_pct * 0.6)) * o
            hi = max(o, c) + wick
            lo = min(o, c) - wick
            volume = abs(self.rng.gauss(self.BASE_VOL, 40)) + 10
            trade_bias = None

            # --- inject order-flow events so setups can fire -----------------
            roll = self.rng.random()
            if self._absorb_left > 0:
                # Continue an absorption burst: price is pinned at a level with
                # repeated long wicks into it on heavy volume and a small body.
                self._absorb_left -= 1
                lvl = self._absorb_level
                tick = lvl * 0.0006
                if self._absorb_side == "bullish":
                    lo = lvl                       # long wick stabs down to level
                    o = lvl + 4 * tick
                    c = lvl + 3 * tick             # small body, rejected back up
                    hi = o + tick
                    trade_bias = 0.74              # buyers absorbing
                else:
                    hi = lvl                       # long wick stabs up to level
                    o = lvl - 4 * tick
                    c = lvl - 3 * tick
                    lo = o - tick
                    trade_bias = 0.26
                volume *= self.rng.uniform(1.8, 2.6)
            elif roll < 0.04:  # liquidity sweep beyond recent extreme, close back in
                if self.rng.random() < 0.5:
                    hi = self.recent_high * 1.001 + wick
                    c = o - abs(ret)            # close back down -> bearish ChoCH
                    trade_bias = 0.35
                else:
                    lo = self.recent_low * 0.999 - wick
                    c = o + abs(ret)            # close back up -> bullish ChoCH
                    trade_bias = 0.65
                volume *= self.rng.uniform(2.2, 3.5)   # >2x avg
            elif roll < 0.07:  # start a 3-candle absorption burst at this level
                self._absorb_left = 3
                self._absorb_side = "bullish" if self.rng.random() < 0.5 else "bearish"
                self._absorb_level = c

            ts = self.t + i * self.tf_ms
            candle = Candle(
                timestamp=ts, open=o, high=max(hi, c, o), low=min(lo, c, o),
                close=c, volume=volume,
                trades=self._make_trades(ts, o, c, volume, trade_bias),
            )
            # update rolling extremes (15-candle horizon proxy)
            self.recent_high = max(candle.high, self.recent_high * 0.98 + candle.high * 0.02)
            self.recent_low = min(candle.low, self.recent_low * 0.98 + candle.low * 0.02)
            self.price = c
            yield candle


# --------------------------------------------------------------------------- #
# Real (ccxt) feed
# --------------------------------------------------------------------------- #
class CCXTFeed:
    """Live/paper/testnet feed backed by Binance USD-M Futures via ccxt.

    Polls closed klines on the configured timeframe and reconstructs
    approximate per-candle trades from recent aggregate trades. This is a
    pragmatic MVP feed — a production system would use the websocket streams
    listed in spec section 4.2.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tf_ms = timeframe_ms(settings.timeframe)
        self._exchange = self._build_exchange()
        self._last_ts: int | None = None
        self._scheduler = CandleScheduler(settings.timeframe)

    def _build_exchange(self):
        import ccxt  # imported lazily so sim mode needs no ccxt install

        opts = {
            "apiKey": self.settings.binance_api_key,
            "secret": self.settings.binance_api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
        ex = ccxt.binanceusdm(opts)
        if self.settings.mode.value == "testnet":
            ex.set_sandbox_mode(True)
            logger.warning("CCXTFeed running against Binance TESTNET sandbox")
        return ex

    def _fetch_trades_for(self, since: int) -> list[Trade]:
        try:
            raw = self._exchange.fetch_trades(
                self.settings.symbol, since=since, limit=1000
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("trade fetch failed: {}", exc)
            return []
        out = []
        for t in raw:
            if t["timestamp"] < since or t["timestamp"] >= since + self.tf_ms:
                continue
            side_is_sell = t.get("side") == "sell"
            out.append(Trade(
                timestamp=t["timestamp"], price=float(t["price"]),
                qty=float(t["amount"]), is_buyer_maker=side_is_sell,
            ))
        return out

    def _fetch_recent(self) -> list[list]:
        """Two most recent klines as ``[ts, o, h, l, c, v, taker_buy_volume]``.

        Uses the raw endpoint so the taker-buy column survives: ccxt's
        ``fetch_ohlcv`` normalises klines to six columns and discards it, and
        that column is what makes CVD real rather than inferred from ticks.
        """
        try:
            raw = self._exchange.fapiPublicGetKlines({
                "symbol": self.settings.symbol.replace("/", ""),
                "interval": self.settings.timeframe, "limit": 2,
            })
            return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                     float(r[4]), float(r[5]), float(r[9])] for r in raw]
        except Exception:  # noqa: BLE001 - fall back to the normalised call
            return [list(r) + [None] for r in self._exchange.fetch_ohlcv(
                self.settings.symbol, timeframe=self.settings.timeframe, limit=2)]

    def candles(self) -> Iterator[Candle]:
        emitted = 0
        limit = self.settings.candle_limit or 10**9
        while emitted < limit:
            try:
                ohlcv = self._fetch_recent()
            except Exception as exc:  # noqa: BLE001 - degrade gracefully, keep polling
                self._scheduler.on_error(exc)
                continue
            if not ohlcv:
                self._scheduler.sleep_until_next_close()
                continue
            # Use the most recent *closed* candle (second-to-last row).
            row = ohlcv[-2]
            ts = int(row[0])
            if ts == self._last_ts:
                # Already have this one — sleep to just after the *next*
                # boundary instead of tight-polling.
                self._scheduler.sleep_until_next_close()
                continue
            self._scheduler.on_success()
            self._last_ts = ts
            taker_buy = row[6] if len(row) > 6 else None
            candle = Candle(
                timestamp=ts, open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5]),
                taker_buy_volume=taker_buy,
                # Only pay for a second REST round-trip when the exchange did
                # not already hand us the buy/sell split.
                trades=[] if taker_buy is not None else self._fetch_trades_for(ts),
            )
            emitted += 1
            yield candle


def build_feed(settings: Settings) -> CandleFeed:
    if settings.uses_real_data:
        if settings.use_websocket:
            logger.info("Using WebSocket feed (mode={})", settings.mode.value)
            from maya.data.websocket_feed import WebSocketFeed
            return WebSocketFeed(settings)
        logger.info("Using CCXT feed (mode={})", settings.mode.value)
        return CCXTFeed(settings)
    if settings.scenario:
        from maya.data.scenarios import ScenarioFeed
        logger.info("Using scripted scenario feed (demo)")
        return ScenarioFeed(settings)
    logger.info("Using simulated feed")
    return SimulatedFeed(settings)
