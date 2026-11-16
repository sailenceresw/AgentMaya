"""Hyperliquid broker on the official ``hyperliquid-python-sdk``.

Selected with ``MAYA_VENUE=hyperliquid MAYA_HL_BROKER=sdk`` in testnet or
live mode. It exists to test order plumbing on a venue that is reachable with
a MetaMask-linked account, without the ``mm`` CLI's 24 to 51 seconds per call.

Safety properties:

- **Testnet unless told twice.** The base URL is the testnet API unless mode
  is live, ``MAYA_LIVE_CONFIRM`` is set, and both ``MAYA_HL_MAINNET=true`` and
  ``MAYA_HL_MAINNET_CONFIRM=I_UNDERSTAND_THE_RISK`` are set.
- **API wallet only.** Orders are signed with ``HL_API_WALLET_KEY``, an API
  (agent) wallet approved for the account at ``HL_ACCOUNT_ADDRESS``. An API
  wallet can place and cancel orders and cannot withdraw. The key is never
  logged.
- **Entry and stop are separate requests.** Hyperliquid has no single call
  here that opens a position with its stop attached, so there is a short
  window after the fill with no stop at the venue. If the stop cannot be
  placed the position is closed at market at once. If the process dies inside
  that window, :meth:`close_unprotected_positions` closes the leftover on the
  next start.
- **A stop is replaced, never removed.** ``update_stop`` places the new stop
  before it cancels the old one.

VERIFIED vs NOT: method names and signatures were checked against
hyperliquid-python-sdk 0.24.0 as installed. Nothing here has been run against
the Hyperliquid testnet; response shapes follow the SDK's examples. The first
run should be ``python -m maya.check_hyperliquid --smoke``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from loguru import logger

from maya.config import HYPERLIQUID_MIN_NOTIONAL_USD, Settings
from maya.execution.metamask_broker import Fill, to_hl_symbol
from maya.execution.models import Position
from maya.strategy.models import TradeIntent

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"


def base_url(settings: Settings) -> str:
    """Testnet unless every mainnet condition holds. One place decides."""
    return MAINNET_URL if settings.hl_mainnet_allowed else TESTNET_URL


def round_price(px: float, sz_decimals: int) -> float:
    """Hyperliquid perps accept at most 5 significant figures and at most
    ``6 - szDecimals`` decimal places."""
    if px <= 0:
        return px
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


def round_size(sz: float, sz_decimals: int) -> float:
    """Round down: rounding up could exceed what the risk layer approved."""
    factor = 10 ** sz_decimals
    return int(sz * factor) / factor


def order_statuses(resp) -> list[dict]:
    """The per-order status list from an ``order`` response, or [] on error."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return []
    return ((resp.get("response") or {}).get("data") or {}).get("statuses") or []


@dataclass
class Clients:
    exchange: object
    info: object


def connect(settings: Settings) -> Clients:
    """Build the SDK clients. Imported lazily so nothing else needs the SDK."""
    if not settings.hl_api_wallet_key or not settings.hl_account_address:
        raise RuntimeError("HL_API_WALLET_KEY and HL_ACCOUNT_ADDRESS must be set in .env")
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    url = base_url(settings)
    wallet = Account.from_key(settings.hl_api_wallet_key)
    logger.info("Hyperliquid SDK: {} for account {}", "MAINNET" if url == MAINNET_URL
                else "testnet", settings.hl_account_address)
    return Clients(
        exchange=Exchange(wallet, url, account_address=settings.hl_account_address),
        info=Info(url, skip_ws=True))


class HyperliquidSDKBroker:
    def __init__(self, settings: Settings, clients: Clients | None = None,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        self.settings = settings
        clients = clients or connect(settings)
        self.exchange, self.info = clients.exchange, clients.info
        self.address = settings.hl_account_address
        self.coin = to_hl_symbol(settings.symbol)
        self._sleep, self._clock = sleep, clock
        self.sz_decimals = self._sz_decimals(self.coin)
        self.last_fill: Fill | None = None
        self._stop_oid: int | None = None
        self._tp_oid: int | None = None

    # -- venue reads -------------------------------------------------------- #
    def _sz_decimals(self, coin: str) -> int:
        for asset in self.info.meta().get("universe", []):
            if asset.get("name") == coin:
                return int(asset["szDecimals"])
        raise RuntimeError(f"{coin} is not a Hyperliquid perp market")

    def position_size(self, coin: str | None = None) -> float:
        """Signed size at the venue: positive long, negative short, 0 flat."""
        coin = coin or self.coin
        for ap in self.info.user_state(self.address).get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0) or 0)
        return 0.0

    def _open_orders(self, coin: str | None = None) -> list[dict]:
        coin = coin or self.coin
        return [o for o in self.info.frontend_open_orders(self.address)
                if o.get("coin") == coin]

    def _has_stop(self, coin: str) -> bool:
        for o in self._open_orders(coin):
            kind = str(o.get("orderType", "")).lower()
            if o.get("isTrigger") and o.get("reduceOnly") and "stop" in kind:
                return True
        return False

    def _log_fill(self, what: str, oid: int | None, assumed_px: float, started: float) -> None:
        """Fill price, fee and latency, next to the price the paper broker
        would have booked. This comparison is the point of the testnet run."""
        latency_ms = (self._clock() - started) * 1000
        fills = [f for f in self.info.user_fills(self.address)
                 if oid is not None and f.get("oid") == oid]
        if not fills:
            logger.info("[HL] {} oid={} latency={:.0f}ms (no fill record yet)",
                        what, oid, latency_ms)
            return
        size = sum(float(f["sz"]) for f in fills)
        px = sum(float(f["px"]) * float(f["sz"]) for f in fills) / size if size else 0.0
        fee = sum(float(f.get("fee", 0) or 0) for f in fills)
        slip_bps = (px / assumed_px - 1) * 10_000 if assumed_px else 0.0
        logger.info("[HL] {} filled {:.6f} @ {:.4f} fee=${:.6f} latency={:.0f}ms | "
                    "paper assumed {:.4f} ({:+.2f} bps)", what, size, px, fee,
                    latency_ms, assumed_px, slip_bps)

    # -- orders ------------------------------------------------------------- #
    def _trigger(self, is_buy: bool, size: float, trigger_px: float, tpsl: str) -> int | None:
        """Place a reduce-only trigger-market order. Returns its oid or None."""
        px = round_price(trigger_px, self.sz_decimals)
        resp = self.exchange.order(
            self.coin, is_buy, size, px,
            {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": tpsl}},
            reduce_only=True)
        for st in order_statuses(resp):
            if "resting" in st:
                return int(st["resting"]["oid"])
            if "error" in st:
                logger.error("[HL] {} order rejected: {}", tpsl, st["error"])
        return None

    def _cancel(self, oid: int | None) -> None:
        if oid is None:
            return
        try:
            self.exchange.cancel(self.coin, oid)
        except Exception as exc:  # noqa: BLE001 - a failed cancel must not mask the caller's work
            logger.warning("[HL] cancel of oid {} failed: {}", oid, exc)

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        self.last_fill = None
        self._stop_oid = self._tp_oid = None
        is_buy = intent.direction == "long"
        size = round_size(qty, self.sz_decimals)
        px = round_price(intent.entry_price, self.sz_decimals)
        if size <= 0 or size * px < HYPERLIQUID_MIN_NOTIONAL_USD:
            logger.warning("[HL] order ${:.2f} is under the ${:.0f} minimum; not sent",
                           size * px, HYPERLIQUID_MIN_NOTIONAL_USD)
            return False

        started = self._clock()
        try:
            self.exchange.update_leverage(leverage, self.coin, is_cross=False)   # isolated
            resp = self.exchange.order(self.coin, is_buy, size, px, {"limit": {"tif": "Gtc"}})
        except Exception as exc:  # noqa: BLE001
            logger.error("[HL] entry failed before it was accepted: {}", exc)
            return False

        statuses = order_statuses(resp)
        if not statuses or "error" in statuses[0]:
            logger.error("[HL] entry rejected: {}", statuses[0].get("error") if statuses else resp)
            return False

        oid: int | None = None
        if "filled" in statuses[0]:
            oid = int(statuses[0]["filled"]["oid"])
        elif "resting" in statuses[0]:
            # A resting entry is waited on and then cancelled. It is never
            # left on the book to fill later with nobody watching.
            oid = int(statuses[0]["resting"]["oid"])
            deadline = started + self.settings.hl_entry_timeout_s
            while self._clock() < deadline and abs(self.position_size()) < size:
                self._sleep(1.0)
            self._cancel(oid)

        filled = abs(self.position_size())
        if filled <= 0:
            logger.info("[HL] entry at {} did not fill within {:.0f}s; cancelled",
                        px, self.settings.hl_entry_timeout_s)
            return False
        self._log_fill("entry", oid, intent.entry_price, started)

        # The position exists and has no stop yet. Stop first, then target.
        self._stop_oid = self._trigger(not is_buy, filled, intent.stop_loss, "sl")
        if self._stop_oid is None:
            logger.critical("[HL] STOP COULD NOT BE PLACED; closing the position now")
            self._flatten()
            return False
        self._tp_oid = self._trigger(not is_buy, filled, intent.take_profit_1, "tp")
        if self._tp_oid is None:
            logger.warning("[HL] target order not placed; the stop is in place and the "
                           "candle loop will take the target")
        self.last_fill = Fill(price=self._entry_price(intent.entry_price), size=filled)
        return True

    def _entry_price(self, fallback: float) -> float:
        for ap in self.info.user_state(self.address).get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == self.coin and pos.get("entryPx"):
                return float(pos["entryPx"])
        return fallback

    def _flatten(self) -> bool:
        """Market-close whatever is open in this coin. True if it ends flat."""
        try:
            self.exchange.market_close(self.coin)
        except Exception as exc:  # noqa: BLE001
            logger.critical("[HL] market close failed: {}", exc)
        flat = abs(self.position_size()) == 0
        if not flat:
            logger.critical("[HL] POSITION STILL OPEN IN {} AFTER A CLOSE ATTEMPT. "
                            "Close it by hand.", self.coin)
        return flat

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        started = self._clock()
        if abs(self.position_size()) > 0:
            # The engine decided to close (time limit, shutdown) or its candle
            # check ran ahead of the venue's trigger. Close what is there.
            self._flatten()
        for oid in (self._stop_oid, self._tp_oid):
            self._cancel(oid)
        for o in self._open_orders():          # anything else left for this coin
            self._cancel(int(o["oid"]))
        fills = self.info.user_fills(self.address)
        last = next((f for f in reversed(fills) if f.get("coin") == self.coin), None)
        self._log_fill(f"exit ({reason})", last.get("oid") if last else None, price, started)
        self._stop_oid = self._tp_oid = None

    def update_stop(self, pos: Position, new_stop: float) -> bool:
        """Replace the stop. The new one is placed before the old one is
        cancelled, so the position is covered throughout."""
        size = abs(self.position_size())
        if size <= 0:
            return False
        new_oid = self._trigger(pos.direction != "long", size, new_stop, "sl")
        if new_oid is None:
            return False            # the old stop is still in place
        self._cancel(self._stop_oid)
        self._stop_oid = new_oid
        return True

    # -- startup safety ----------------------------------------------------- #
    def close_unprotected_positions(self) -> list[str]:
        """Close every position that has no reduce-only stop at the venue.

        Covers a crash between an entry fill and its stop. Returns the coins
        it closed. A position that does have a stop is left alone; the normal
        startup mismatch check decides what to do about it.
        """
        closed: list[str] = []
        for ap in self.info.user_state(self.address).get("assetPositions", []):
            pos = ap.get("position", {})
            coin, size = pos.get("coin"), float(pos.get("szi", 0) or 0)
            if not coin or size == 0 or self._has_stop(coin):
                continue
            logger.critical("[HL] {} position of {} has NO STOP at the venue; closing it",
                            coin, size)
            try:
                self.exchange.market_close(coin)
                closed.append(coin)
            except Exception as exc:  # noqa: BLE001
                logger.critical("[HL] could not close unprotected {}: {}", coin, exc)
        return closed
