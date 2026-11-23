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

- **Exits are booked at the venue's fill.** ``close_position`` sets
  ``last_exit`` from this trade's own fills, and the engine journals that
  price and fee instead of the planned one.

VERIFIED vs NOT: method names and signatures were checked against
hyperliquid-python-sdk 0.24.0 as installed. Signed orders ran on the
Hyperliquid testnet 18-23 Nov 2026 (see docs/RUNNING.md); never on mainnet.
The tests use an invented stand-in venue. The first run on a new account
should be ``python -m maya.check_hyperliquid --smoke``.
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


def rejection(resp) -> str | None:
    """The venue's reason when a signed action was refused, else None.

    The SDK does not raise on a refusal; it returns it. Seen on testnet (Nov
    2026) from ``market_close`` with an API wallet that was no longer
    approved: ``{"status": "err", "response": "User or API Wallet 0x... does
    not exist."}``. ``market_close`` returns None when there is nothing to close.
    """
    if not isinstance(resp, dict):
        return None
    if resp.get("status") != "ok":
        return str(resp.get("response") or resp)
    for st in order_statuses(resp):
        if "error" in st:
            return str(st["error"])
    return None


@dataclass(frozen=True)
class ExitFill:
    """This trade's exit as the venue filled it, in the signal's price space.

    ``reason`` is ``stop_loss`` or ``take_profit`` when one of the trade's own
    venue triggers did the filling, else None (a market close by the engine).
    """

    price: float
    size: float
    fee: float
    reason: str | None = None


def aggregate(fills: list[dict]) -> tuple[float, float, float] | None:
    """Size-weighted price, total size and total fee of ``fills``."""
    size = sum(float(f["sz"]) for f in fills)
    if size <= 0:
        return None
    px = sum(float(f["px"]) * float(f["sz"]) for f in fills) / size
    fee = sum(float(f.get("fee", 0) or 0) for f in fills)
    return px, size, fee


@dataclass
class Clients:
    exchange: object
    info: object
    # Read-only info client for the real (mainnet) market. Set only on testnet
    # with ``hl_testnet_price_map`` on; its presence is what turns the price
    # map on, so a broker built without it sends prices unchanged.
    reference: object | None = None


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
    reference = None
    if url == TESTNET_URL and settings.hl_testnet_price_map:
        reference = Info(MAINNET_URL, skip_ws=True)      # public reads only, no key
    return Clients(
        exchange=Exchange(wallet, url, account_address=settings.hl_account_address),
        info=Info(url, skip_ws=True), reference=reference)


class HyperliquidSDKBroker:
    def __init__(self, settings: Settings, clients: Clients | None = None,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        self.settings = settings
        clients = clients or connect(settings)
        self.exchange, self.info = clients.exchange, clients.info
        self._reference = clients.reference
        # Venue price / signal price for the open trade. 1.0 off testnet.
        self._ratio = 1.0
        self.address = settings.hl_account_address
        self.coin = to_hl_symbol(settings.symbol)
        self._sleep, self._clock = sleep, clock
        self.sz_decimals = self._sz_decimals(self.coin)
        self.last_fill: Fill | None = None
        self.last_exit: ExitFill | None = None
        self._stop_oid: int | None = None
        self._tp_oid: int | None = None
        self.unclosed: dict[str, str] = {}
        self.leftover_orders: list[int] = []   # orders still open after the last close

    # -- venue reads -------------------------------------------------------- #
    def _sz_decimals(self, coin: str) -> int:
        for asset in self.info.meta().get("universe", []):
            if asset.get("name") == coin:
                return int(asset["szDecimals"])
        raise RuntimeError(f"{coin} is not a Hyperliquid perp market")

    def _use(self, symbol: str) -> bool:
        """Point the broker at ``symbol``'s coin. False if the venue lists none.

        Regression: the coin was fixed from MAYA_SYMBOL at start, so under the
        multi-coin scanner an ETH signal would have been sent as a BTC order
        at ETH's prices. Every order now follows the trade's own symbol.
        """
        coin = to_hl_symbol(symbol)
        if coin == self.coin:
            return True
        try:
            decimals = self._sz_decimals(coin)
        except Exception as exc:  # noqa: BLE001 - not listed here (testnet lists fewer)
            logger.error("[HL] {} cannot be traded on this venue: {}", coin, exc)
            return False
        self.coin, self.sz_decimals = coin, decimals
        return True

    def position_size(self, coin: str | None = None) -> float:
        """Signed size at the venue: positive long, negative short, 0 flat."""
        coin = coin or self.coin
        for ap in self.info.user_state(self.address).get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0) or 0)
        return 0.0

    def venue_size(self, symbol: str) -> float | None:
        """Absolute size the venue holds in ``symbol``, or None if unreadable.

        None is not 0: a failed read must never be taken for "flat", which
        would book a close while the position is still open at the venue.
        """
        try:
            return abs(self.position_size(to_hl_symbol(symbol)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HL] could not read the {} position: {}", symbol, exc)
            return None

    def _price_ratio(self) -> float | None:
        """Testnet mid over real mid, or 1.0 when prices are not mapped.

        None means the two prices could not be compared, and the caller must
        not trade: unmapped prices on testnet put the stop on the wrong side
        of the market. The reference is Hyperliquid mainnet, which sat 1 bp
        from the Binance price the signals are built on when this was checked.
        """
        if self._reference is None:
            return 1.0
        try:
            venue = float(self.info.all_mids()[self.coin])
            real = float(self._reference.all_mids()[self.coin])
            ratio = venue / real
        except Exception as exc:  # noqa: BLE001
            logger.error("[HL] testnet price map unavailable, entry skipped: {}", exc)
            return None
        if not 0.8 <= ratio <= 1.25:
            logger.error("[HL] testnet {} at {} is too far from the real {} to map; "
                         "entry skipped", self.coin, venue, real)
            return None
        logger.info("[HL] testnet {} mid {} vs real {} ({:+.0f} bps): order prices scaled "
                    "by {:.5f}", self.coin, venue, real, (ratio - 1) * 10_000, ratio)
        return ratio

    def _thin_book(self, size: float, mid: float) -> str | None:
        """Why the book cannot take ``size`` both ways within the slippage
        cap, or None if it can.

        Both sides, because a position that cannot be closed is worse than
        one never opened. Regression (testnet, 20 Nov 2026): HYPE's testnet
        book was nearly empty; two shorts filled 15% away from the mid, every
        close was refused, and the position was later liquidated.
        """
        try:
            levels = self.info.l2_snapshot(self.coin)["levels"]
        except Exception as exc:  # noqa: BLE001
            return f"order book unavailable: {exc}"
        slip = self.settings.perps_max_slippage_bps / 10_000
        bids = sum(float(lv["sz"]) for lv in levels[0] if float(lv["px"]) >= mid * (1 - slip))
        asks = sum(float(lv["sz"]) for lv in levels[1] if float(lv["px"]) <= mid * (1 + slip))
        if min(bids, asks) < size:
            return (f"book too thin within {self.settings.perps_max_slippage_bps:.0f} bps of "
                    f"mid: {bids:g} bid / {asks:g} ask for size {size:g}")
        return None

    def _fill_keys(self) -> set[tuple]:
        try:
            return {(f.get("oid"), f.get("px"), f.get("sz"), f.get("time"))
                    for f in self.info.user_fills(self.address)}
        except Exception:  # noqa: BLE001 - only used to tell new fills from old
            return set()

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

    def _log_fill(self, what: str, oid: int | set | None, assumed_px: float, started: float,
                  fills: list[dict] | None = None) -> tuple[float, float, float] | None:
        """Fill price, fee and latency, next to the price the paper broker
        would have booked. This comparison is the point of the testnet run.
        Returns the aggregate (price, size, fee), or None with no fill record."""
        latency_ms = (self._clock() - started) * 1000
        oids = oid if isinstance(oid, set) else {oid} - {None}
        if fills is None:
            fills = [f for f in self.info.user_fills(self.address) if f.get("oid") in oids]
        agg = aggregate(fills)
        if agg is None:
            logger.info("[HL] {} oid={} latency={:.0f}ms (no fill record yet)",
                        what, oid, latency_ms)
            return None
        px, size, fee = agg
        slip_bps = (px / assumed_px - 1) * 10_000 if assumed_px else 0.0
        logger.info("[HL] {} filled {:.6f} @ {:.4f} fee=${:.6f} latency={:.0f}ms | "
                    "paper assumed {:.4f} ({:+.2f} bps)", what, size, px, fee,
                    latency_ms, assumed_px, slip_bps)
        return agg

    def _exit_fills(self, own: set[int], seen: set[tuple]) -> list[dict]:
        """This trade's exit fills: its own stop or target, or fills that
        appeared during this close. Regression: the newest fill for the coin
        was taken, which after a refused close was the ENTRY fill, and on
        PUMP an older trade's exit (77489 @ 0.0026, logged three times)."""
        return [f for f in self.info.user_fills(self.address)
                if f.get("coin") == self.coin
                and (f.get("oid") in own
                     or (f.get("oid"), f.get("px"), f.get("sz"), f.get("time")) not in seen)]

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
            # Like orders, a refused cancel comes back in the response.
            reason = rejection(self.exchange.cancel(self.coin, oid))
        except Exception as exc:  # noqa: BLE001 - a failed cancel must not mask the caller's work
            reason = str(exc)
        if reason:
            logger.warning("[HL] cancel of oid {} failed: {}", oid, reason)

    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        self.last_fill = None
        self._stop_oid = self._tp_oid = None
        if not self._use(intent.symbol):
            return False
        is_buy = intent.direction == "long"
        size = round_size(qty, self.sz_decimals)
        held = self.position_size()
        if held != 0:
            # Regression (testnet, 20 Nov 2026): the engine believed HYPE was
            # flat after a refused close, opened another short, and the venue
            # added it to the stuck one.
            logger.critical("[HL] venue already holds {} {}; no new entry until it is "
                            "closed", held, self.coin)
            return False
        ratio = self._price_ratio()
        if ratio is None:
            return False
        try:
            mid_now = float(self.info.all_mids()[self.coin])
        except Exception as exc:  # noqa: BLE001
            logger.error("[HL] no mid for {}, entry skipped: {}", self.coin, exc)
            return False
        thin = self._thin_book(size, mid_now)
        if thin:
            logger.error("[HL] {} entry skipped: {}", self.coin, thin)
            return False
        self._ratio = ratio
        px = round_price(intent.entry_price * ratio, self.sz_decimals)
        if size <= 0 or size * px < HYPERLIQUID_MIN_NOTIONAL_USD:
            logger.warning("[HL] order ${:.2f} is under the ${:.0f} minimum; not sent",
                           size * px, HYPERLIQUID_MIN_NOTIONAL_USD)
            return False

        started = self._clock()
        try:
            self.exchange.update_leverage(leverage, self.coin, is_cross=False)   # isolated
            if self.settings.perps_order_type == "market":
                # Hyperliquid has no market order type: a market order is an
                # IOC limit priced through the book. What does not fill at
                # once is cancelled by the venue, never left resting.
                slip = self.settings.perps_max_slippage_bps / 10_000
                mid = float(self.info.all_mids()[self.coin])
                cap = round_price(mid * (1 + slip if is_buy else 1 - slip), self.sz_decimals)
                resp = self.exchange.order(self.coin, is_buy, size, cap, {"limit": {"tif": "Ioc"}})
            else:
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
        entry = self._log_fill("entry", oid, px, started)

        # The position exists and has no stop yet. Stop first, then target.
        self._stop_oid = self._trigger(not is_buy, filled, intent.stop_loss * ratio, "sl")
        if self._stop_oid is None:
            logger.critical("[HL] STOP COULD NOT BE PLACED; closing the position now")
            self._flatten()
            return False
        self._tp_oid = self._trigger(not is_buy, filled, intent.take_profit_1 * ratio, "tp")
        if self._tp_oid is None:
            logger.warning("[HL] target order not placed; the stop is in place and the "
                           "candle loop will take the target")
        # Reported in the signal's price space, so the engine's entry, stop
        # and target all stay on one scale.
        self.last_fill = Fill(price=self._entry_price(px) / ratio, size=filled,
                              fee=entry[2] if entry else None)
        return True

    def _entry_price(self, fallback: float) -> float:
        for ap in self.info.user_state(self.address).get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == self.coin and pos.get("entryPx"):
                return float(pos["entryPx"])
        return fallback

    def _flatten(self) -> bool:
        """Market-close whatever is open in this coin. True if it ends flat."""
        reason = None
        try:
            reason = rejection(self.exchange.market_close(self.coin))
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
        flat = abs(self.position_size()) == 0
        if not flat:
            logger.critical("[HL] POSITION STILL OPEN IN {} AFTER A CLOSE ATTEMPT ({}). "
                            "Close it by hand.", self.coin, reason or "no reason given")
        return flat

    def close_position(self, pos: Position, price: float, reason: str) -> bool:
        """Close at the venue. False if the venue still shows the position.

        On False the stop and target stay where they are and the caller
        must keep treating the position as open. Regression (testnet, 20 Nov
        2026): a refused close was followed by cancelling both orders and
        journaling the trade as closed; the unprotected HYPE short was then
        liquidated a day later.
        """
        self._use(pos.symbol)          # a no-op for the coin it opened; listed, since it did
        self.last_exit = None
        started = self._clock()
        seen = self._fill_keys()
        if abs(self.position_size()) > 0:
            # The engine decided to close (time limit, shutdown) or its candle
            # check ran ahead of the venue's trigger. Close what is there.
            if not self._flatten():
                logger.critical("[HL] {} close refused; its stop and target are LEFT IN "
                                "PLACE and the position stays open", self.coin)
                return False
        for oid in (self._stop_oid, self._tp_oid):
            self._cancel(oid)
        for o in self._open_orders():          # anything else left for this coin
            self._cancel(int(o["oid"]))
        # A reduce-only stop or target left behind can fire against the NEXT
        # position in this coin and close it unasked, so leftovers are checked
        # and reported, not assumed gone. A restart also refuses to start with
        # them open (startup reconciliation).
        try:
            self.leftover_orders = [int(o["oid"]) for o in self._open_orders()]
        except Exception as exc:  # noqa: BLE001 - the check itself must not break the close
            logger.warning("[HL] could not re-check open orders after the close: {}", exc)
            self.leftover_orders = []
        if self.leftover_orders:
            logger.critical("[HL] {} order(s) still open on {} after the close: {}. Cancel "
                            "them by hand; they could close a later position.",
                            len(self.leftover_orders), self.coin, self.leftover_orders)
        own = {self._stop_oid, self._tp_oid} - {None}
        exits = self._exit_fills(own, seen)
        # The venue can list a fill a moment after the position reads flat.
        # A short wait here is what lets the journal book the real price.
        for _ in range(3):
            if exits:
                break
            self._sleep(1.0)
            exits = self._exit_fills(own, seen)
        self._log_fill(f"exit ({reason})", None, price * self._ratio, started, fills=exits)
        agg = aggregate(exits)
        if agg is not None:
            px, size, fee = agg
            oids = {f.get("oid") for f in exits}
            venue_reason = ("stop_loss" if self._stop_oid in oids
                            else "take_profit" if self._tp_oid in oids else None)
            self.last_exit = ExitFill(price=px / self._ratio, size=size, fee=fee,
                                      reason=venue_reason)
        else:
            logger.warning("[HL] {} exit ({}) has no fill record; the engine books the "
                           "planned price. Check the venue's fill history.", self.coin, reason)
        self._stop_oid = self._tp_oid = None
        return True

    def update_stop(self, pos: Position, new_stop: float) -> bool:
        """Replace the stop. The new one is placed before the old one is
        cancelled, so the position is covered throughout."""
        if not self._use(pos.symbol):
            return False
        size = abs(self.position_size())
        if size <= 0:
            return False
        new_oid = self._trigger(pos.direction != "long", size, new_stop * self._ratio, "sl")
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

        A coin counts as closed only when the venue then shows it flat.
        Regression: a refused close (the SDK returns the refusal, it does not
        raise) was reported as closed. Coins still open are in ``unclosed``
        with the venue's reason.
        """
        closed: list[str] = []
        self.unclosed: dict[str, str] = {}
        for ap in self.info.user_state(self.address).get("assetPositions", []):
            pos = ap.get("position", {})
            coin, size = pos.get("coin"), float(pos.get("szi", 0) or 0)
            if not coin or size == 0 or self._has_stop(coin):
                continue
            logger.critical("[HL] {} position of {} has NO STOP at the venue; closing it",
                            coin, size)
            try:
                reason = rejection(self.exchange.market_close(coin))
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
            if reason is None and self.position_size(coin) == 0:
                closed.append(coin)
                continue
            self.unclosed[coin] = reason or "still open after the close"
            logger.critical("[HL] could not close unprotected {}: {}", coin, self.unclosed[coin])
        return closed
