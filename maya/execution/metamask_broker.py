"""Broker that routes orders to Hyperliquid perpetuals via the MetaMask
Agent Wallet ``mm`` CLI.

Why this exists: Maya was built against Binance USD-M futures, which is a
*centralized* venue authenticated with an API key/secret against a
Binance-held balance. A MetaMask wallet is self-custodial and on-chain — it
cannot place a Binance order at all. MetaMask Agent Wallet *can* trade
perpetuals on Hyperliquid, which is the closest analogue to what Maya
already does (BTC perps, leverage, exchange-held SL/TP). So this broker
swaps the *venue*, not the strategy: layers 2-4 (signals, setups,
confluence, risk) are untouched and venue-agnostic.

Transport is the ``mm`` CLI rather than an SDK because MetaMask ships no
Python SDK — every call shells out to ``mm ... --json`` and parses the
``{"ok": bool, "data"|"error": ...}`` envelope.

**Verification status.** The command names, flags and the outer ``ok``/``data``
envelope were read off ``mm --help --json`` from the installed CLI (v6.0.0),
and re-checked against v7.1.0 — including the order result shape (``status``
``filled``/``resting``/``submitted``/``rejected``), the stderr error envelope
and the line-delimited stream form; see :func:`parse_output` and
``docs/RUNNING.md`` "CLI v7.1 update". Against a live authenticated session on Hyperliquid
testnet:

* ``perps markets`` parsing is **confirmed** — a real mark price (64773.00 for
  BTC) was extracted through the tolerant key lookup below.
* ``perps balance`` is confirmed against the real field names; ``perps
  positions`` has only been observed empty. Its populated row shape
  (``symbol``, ``side``, absolute ``size``, ``entryPrice``) is taken from the
  v7.1.0 CLI source, not from a live position.
* No order has ever been signed or filled through this module.

Field extraction is therefore deliberately tolerant (it tries several plausible
key spellings) and every parse has a fallback. Treat
:meth:`MetaMaskPerpsBroker.fetch_position` and
:meth:`MetaMaskPerpsBroker.fetch_collateral_usd` as unproven until they have
returned a non-zero result against a funded testnet account — a silent zero
from either is exactly what a wrong key name would produce, and
``fetch_collateral_usd`` feeds the risk manager's starting equity.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from maya.config import HYPERLIQUID_MIN_NOTIONAL_USD, MIN_SIZE_HEADROOM, Settings
from maya.execution.models import Position
from maya.strategy.models import TradeIntent

# Quote suffixes stripped when mapping a Maya symbol to a Hyperliquid one.
# Maya uses Binance-style "BTCUSDT"; Hyperliquid markets are bare coins ("BTC").
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "PERP")


class MMError(RuntimeError):
    """A ``mm`` invocation failed, timed out, or returned ``ok: false``.

    ``code`` carries the CLI's machine-readable error code (``AUTH_FAILED``,
    ``INSUFFICIENT_FUNDS``, ...) when it reported one.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Fill:
    """What the venue says an entry actually filled at."""

    price: float
    size: float


@dataclass
class MMResult:
    """One `mm` invocation's outcome, from whichever output form it used."""

    ok: bool
    data: Any = None
    error: Any = None
    notices: list[dict[str, Any]] = field(default_factory=list)


def mm_env(settings: Settings) -> dict[str, str]:
    """Environment for the ``mm`` subprocess.

    Telemetry is disabled unless explicitly enabled: it is the difference
    between a 1.7s call and one that hangs after printing its result.
    """
    env = dict(os.environ)
    if not settings.mm_telemetry:
        env["MM_TELEMETRY_DISABLED"] = "1"
    return env


def parse_output(stdout: str | None, stderr: str | None) -> MMResult | None:
    """Read a ``mm --json`` result from either output stream.

    The CLI (v7.1) has two output forms, both observed:

    * **Envelope** — one ``{"ok": bool, "data"|"error": ...}`` document,
      pretty-printed across several lines. Success goes to *stdout* but a
      failure goes to **stderr** (``{"ok": false, "error": {"code":
      "AUTH_FAILED", "message": ..., "hint": ...}}``).
    * **Stream** — once a command has emitted anything mid-run (for example a
      ``{"_notice": {"kind": "AWAITING_MFA", ...}}`` line while a Guard Mode
      approval is pending), output becomes one JSON object per line and ends
      with ``{"_summary": data}`` on stdout or ``{"_error": error}`` on stderr.

    Returns ``None`` when neither stream holds a conclusive result.
    """
    docs = _json_docs(stdout) + _json_docs(stderr)
    notices = [d["_notice"] for d in docs
               if isinstance(d, dict) and isinstance(d.get("_notice"), dict)]
    for doc in docs:
        if isinstance(doc, dict) and "ok" in doc:
            ok = bool(doc.get("ok"))
            return MMResult(ok=ok, data=doc.get("data"),
                            error=None if ok else doc.get("error", doc), notices=notices)
    for doc in docs:
        if isinstance(doc, dict) and "_error" in doc:
            return MMResult(ok=False, error=doc["_error"], notices=notices)
    for doc in docs:
        if isinstance(doc, dict) and "_summary" in doc:
            return MMResult(ok=True, data=doc["_summary"], notices=notices)
    return None


def _json_docs(text: str | None) -> list[Any]:
    """Every JSON document in ``text``: the whole text, else one per line."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    docs = []
    for line in text.splitlines():
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return docs


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _log_notice(printable: str, notice: dict[str, Any]) -> None:
    if notice.get("kind") == "AWAITING_MFA":
        logger.warning(
            "`{}` waited on a MetaMask approval (polling id {}, expires {}): {}",
            printable, notice.get("pollingId", "?"), notice.get("expiresAt", "?"),
            notice.get("message", "approve it in the MetaMask app"))
    else:
        logger.info("`{}` notice: {}", printable, notice)


def to_hl_symbol(symbol: str) -> str:
    """``BTCUSDT`` / ``BTC/USDT:USDT`` -> ``BTC``.

    Hyperliquid names markets by the base coin alone. Maya carries
    Binance-style symbols throughout, so this is the single mapping point.
    """
    s = symbol.upper().strip()
    # ccxt unified form ("BTC/USDT:USDT") — the base is everything before "/".
    if "/" in s:
        return s.split("/", 1)[0]
    for suffix in _QUOTE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def resolve_mm_binary() -> str:
    """Absolute path to the ``mm`` launcher.

    On Windows an npm global install drops three files next to each other:
    an extensionless shell script, ``mm.cmd`` and ``mm.ps1``. Only the
    ``.cmd`` is directly executable by :mod:`subprocess` without a shell, and
    a bare ``shutil.which("mm")`` can return the extensionless one — so the
    Windows-specific candidates are tried first.
    """
    if os.name == "nt":
        for candidate in ("mm.cmd", "mm.exe", "mm.bat"):
            found = shutil.which(candidate)
            if found:
                return found
    found = shutil.which("mm")
    if found:
        return found
    raise MMError(
        "the MetaMask Agent Wallet CLI ('mm') is not on PATH — install it with "
        "`npm install -g @metamask/agent-wallet@latest`"
    )


class MetaMaskPerpsBroker:
    """Places Hyperliquid perp orders through the MetaMask agent wallet.

    Implements the same :class:`~maya.execution.brokers.Broker` protocol as
    :class:`~maya.execution.brokers.CCXTBroker` so the execution engine is
    unchanged.

    One structural difference from the Binance path is worth understanding
    before trusting this with money: ``mm perps open`` has **no stop-loss or
    take-profit flags**. The protective orders can only be attached by a
    *second* call (``mm perps modify --sl --tp``). Between those two calls
    the position is live and unprotected. This class therefore treats a
    failed ``modify`` as fatal and immediately closes the position rather
    than leaving a naked one on the book — see :meth:`open_position`.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.binary = resolve_mm_binary()
        # Maya's "testnet" mode maps to Hyperliquid testnet; live maps to
        # mainnet. Anything else has no business placing real orders.
        self.network = "testnet" if settings.mode.value == "testnet" else "mainnet"
        self.venue = settings.perps_venue
        self.dry_run = settings.perps_dry_run
        self.tag = self.network.upper() + (" DRY-RUN" if self.dry_run else "")
        self.timeout_s = settings.mm_timeout_s
        self._market_cache: dict[str, dict[str, Any]] = {}
        # Environment for the `mm` subprocess only; Maya's own env is untouched.
        self._env = mm_env(settings)
        # The venue's report of the most recent successful entry, read by the
        # execution engine so the journal records the real fill rather than
        # the intended price. None when the venue did not report one.
        self.last_fill: Fill | None = None

    # -- transport -------------------------------------------------------- #
    def _run(self, *args: str, timeout: float | None = None,
             signing: bool = False) -> dict[str, Any]:
        """Invoke ``mm`` and return the parsed ``data`` payload.

        ``signing`` marks commands that submit a wallet job (open, close,
        modify, cancel); they get ``--wallet-timeout`` so the CLI abandons a
        stalled signature or MFA approval itself, with an error saying so,
        before this process's own timeout fires.

        Raises :class:`MMError` on a timeout with nothing printed, unparseable
        output, or a failed result.
        """
        flags = list(args)
        if signing:
            flags += ["--wallet-timeout", str(int(self.settings.mm_wallet_timeout_s))]
        cmd = [self.binary, *flags, "--json"]
        printable = " ".join(["mm", *flags, "--json"])
        limit = timeout if timeout is not None else self.timeout_s
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=limit, check=False,
                env=self._env,
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            # The CLI prints its result and *then* flushes analytics, and that
            # flush has been seen to hang long after the work was done. So a
            # timeout is only ambiguous if nothing conclusive was printed.
            stdout, stderr, code = _text(exc.stdout), _text(exc.stderr), None
            if parse_output(stdout, stderr) is None:
                raise MMError(
                    f"`{printable}` timed out after {limit}s — the order state "
                    f"is UNKNOWN; check `mm perps positions` before retrying"
                ) from exc
            logger.warning("`{}` printed its result but did not exit within {}s — "
                           "using the printed result", printable, limit)

        # This CLI is slow enough to matter on a 1m timeframe, so every call's
        # cost is logged rather than hidden. If these numbers are large, the
        # strategy timeframe — not the timeout — is what needs revisiting.
        elapsed = time.monotonic() - started
        if elapsed > 10:
            logger.warning("`{}` took {:.1f}s — slow enough to affect entry quality "
                           "on a {} timeframe", printable, elapsed, self.settings.timeframe)
        else:
            logger.debug("`{}` took {:.1f}s", printable, elapsed)

        result = parse_output(stdout, stderr)
        if result is None:
            out = (stdout or "").strip()
            if out:
                raise MMError(f"`{printable}` returned non-JSON output: {out[:400]}")
            raise MMError(
                f"`{printable}` produced no output (exit {code}): "
                f"{(stderr or '').strip()[:400]}"
            )

        for notice in result.notices:
            _log_notice(printable, notice)

        if not result.ok:
            raise MMError(f"`{printable}` failed: {_describe_error(result.error)}",
                          code=_error_code(result.error))

        # `data` is not always an object: `perps positions` and `perps orders`
        # return a bare JSON array (confirmed: `{"ok":true,"data":[]}`). It is
        # wrapped under "result" so every caller gets a dict, and `as_list`
        # knows to look there.
        data = result.data
        return data if isinstance(data, dict) else {"result": data}

    def perps(self, *args: str, timeout: float | None = None,
              signing: bool = False) -> dict[str, Any]:
        """``mm perps <args>`` with the venue/network flags this broker is bound to."""
        return self._run(
            "perps", *args,
            "--venue", self.venue,
            "--network", self.network,
            timeout=timeout, signing=signing,
        )

    def auth_status(self) -> dict[str, Any]:
        """``mm auth status`` — whether the CLI session can sign anything."""
        return self._run("auth", "status")

    def doctor(self) -> dict[str, Any]:
        """``mm doctor`` — CLI version and session health. Needs no session.

        Shape confirmed against v7.1.0::

            {"cli": "7.1.0", "env": "prod", "authenticated": false,
             "initialized": false, "recommendedSkills": {...},
             "hints": ["Not authenticated for env prod. Run `mm login`."]}
        """
        return self._run("doctor")

    def wallet_info(self) -> dict[str, str | None]:
        """The active server wallet's address and trading mode.

        ``mm wallet trading-mode get`` returns ``{"mode": ..., "address": ...}``
        (per the v7.1.0 source), so one call answers both."""
        data = self._run("wallet", "trading-mode", "get")
        mode = pick(data, "mode", "tradingMode", default=None)
        address = pick(data, "address", default=None)
        return {"mode": str(mode) if mode is not None else None,
                "address": str(address) if address is not None else None}

    def pulse(self, symbol: str) -> dict[str, Any]:
        """MetaMask Pulse: an AI-written summary of recent news and trends for
        a Hyperliquid perps market (``mm pulse asset --hl-perps-market``).

        Context for a human or the LLM veto, never a signal on its own — it
        is text about the market, with no measured predictive value here.
        """
        return self._run("pulse", "asset", "--hl-perps-market", to_hl_symbol(symbol))

    def market_meta(self, symbol: str) -> dict[str, Any]:
        """Venue metadata for ``symbol``, fetched once and cached.

        Confirmed shape from a live ``mm perps markets --symbol BTC``::

            {"venue": "hyperliquid", "symbol": "BTC", "maxLeverage": 40,
             "sizeDecimals": 5, "markPrice": "64849.0",
             "oraclePrice": "65044.9", "fundingRate": "-0.0000693105",
             "openInterest": "61.64058", "volume24h": "..."}

        Cached deliberately: every ``mm`` call costs tens of seconds, so this
        must not be on the per-order path.
        """
        sym = to_hl_symbol(symbol)
        if sym in self._market_cache:
            return self._market_cache[sym]

        data = self.perps("markets", "--symbol", sym)
        for entry in as_list(data, "markets"):
            if isinstance(entry, dict) and to_hl_symbol(
                    str(pick(entry, "symbol", "coin", default=""))) == sym:
                self._market_cache[sym] = entry
                return entry
        raise MMError(f"{sym} not found in `mm perps markets` on {self.network}")

    def mark_price(self, symbol: str) -> float | None:
        """Current mark price, fetched fresh (not from the metadata cache)."""
        sym = to_hl_symbol(symbol)
        for entry in as_list(self.perps("markets", "--symbol", sym), "markets"):
            if isinstance(entry, dict) and to_hl_symbol(
                    str(pick(entry, "symbol", "coin", default=""))) == sym:
                price = as_float(pick(entry, "markPrice", "markPx", default=0.0))
                return price or None
        return None

    def size_decimals_for(self, symbol: str) -> int:
        """Venue size precision, read from the venue rather than assumed.

        Hyperliquid rejects orders carrying more size decimals than the
        market's ``sizeDecimals``, and that value differs per market — so
        hardcoding the BTC value (5) would silently break every other symbol.
        Falls back to the configured default if the lookup fails, since an
        order with a good-enough size beats no order at all.
        """
        try:
            meta = self.market_meta(symbol)
        except MMError as exc:
            logger.warning("could not read sizeDecimals for {} ({}); using "
                           "configured default {}", symbol, exc,
                           self.settings.perps_size_decimals)
            return self.settings.perps_size_decimals
        value = pick(meta, "sizeDecimals", "szDecimals", default=None)
        return int(value) if value is not None else self.settings.perps_size_decimals

    def check_leverage_supported(self, symbol: str, leverage: int) -> None:
        """Warn if the requested leverage exceeds what the market allows."""
        try:
            max_lev = as_float(pick(self.market_meta(symbol), "maxLeverage", default=0.0))
        except MMError:
            return
        if max_lev and leverage > max_lev:
            logger.error("{} allows at most {:.0f}x on {} but {}x was requested — "
                         "the venue will reject this order",
                         to_hl_symbol(symbol), max_lev, self.network, leverage)

    # -- Broker protocol -------------------------------------------------- #
    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        self.last_fill = None
        sym = to_hl_symbol(intent.symbol)
        side = "long" if intent.direction == "long" else "short"
        # Precision and leverage limits come from the venue (cached, so this
        # costs one call per symbol per process, not one per order).
        decimals = self.size_decimals_for(intent.symbol)
        if self.settings.min_size_trading:
            # Round UP: rounding the ~$10.50 target down could land under
            # the $10 minimum. The overshoot is at most one size increment.
            size = _format_size_up(
                HYPERLIQUID_MIN_NOTIONAL_USD * MIN_SIZE_HEADROOM / intent.entry_price, decimals)
        else:
            size = _format_size(qty, decimals)
        self.check_leverage_supported(intent.symbol, leverage)
        notional = float(size) * intent.entry_price
        if notional < HYPERLIQUID_MIN_NOTIONAL_USD:
            logger.error("[{}] {} order of ${:.2f} notional is below Hyperliquid's "
                         "${:.0f} minimum — not sent", self.tag, sym, notional,
                         HYPERLIQUID_MIN_NOTIONAL_USD)
            return False

        open_args = [
            "open",
            "--symbol", sym,
            "--side", side,
            "--size", size,
            "--leverage", str(int(leverage)),
        ]
        if self.settings.perps_order_type == "limit":
            # Match CCXTBroker semantics: Maya's R:R is computed off
            # intent.entry_price, so entering at that exact price keeps the
            # planned risk honest.
            open_args += ["--type", "limit", "--limit-px", _format_price(intent.entry_price)]
        else:
            open_args += ["--type", "market",
                          "--max-slippage-bps", str(int(self.settings.perps_max_slippage_bps))]

        if self.dry_run:
            return self._rehearse_open(intent, open_args, sym, side, size, leverage)

        try:
            result = self.perps(*open_args, "--yes", signing=True)
        except MMError as exc:
            logger.error("[{}] hyperliquid open failed for {}: {}",
                         self.network.upper(), sym, exc)
            return False

        # v7 reports what happened to the order: {"orderId": "...",
        # "status": "filled"|"resting"|"submitted"|"rejected",
        # "averagePrice": "...", "filledSize": "...", "error": "..."}.
        status = str(result.get("status", "")).lower()
        order_id = result.get("orderId")
        if status == "rejected":
            logger.error("[{}] hyperliquid rejected the {} {} entry: {}",
                         self.network.upper(), side, sym, result.get("error", "no reason given"))
            return False

        fill: Fill | None = None
        if status == "filled":
            fill = _fill_from(result, "averagePrice", "filledSize")
        if fill is None:
            # Resting (a limit below/above the market), submitted without a
            # status, or a filled report missing its numbers. Until the venue
            # shows a position, the entry is not protected by anything: SL/TP
            # can only attach to a position, never to a resting order.
            fill = self._await_entry(sym, order_id, float(size), intent.entry_price)
            if fill is None:
                return False

        self.last_fill = fill
        logger.info("[{}] OPEN {} {} size={} lev={}x fill={:.2f} (planned {:.2f})",
                    self.network.upper(), side, sym, _format_size(fill.size, 8).rstrip("0"),
                    leverage, fill.price, intent.entry_price)

        # Attach SL/TP. This is a separate round-trip and the position is
        # unprotected until it lands — if it fails, unwind immediately.
        try:
            self.perps(
                "modify",
                "--symbol", sym,
                "--sl", _format_price(intent.stop_loss),
                "--tp", _format_price(intent.take_profit_1),
                "--yes",
                signing=True,
            )
        except MMError as exc:
            logger.error(
                "[{}] SL/TP attach FAILED for {} ({}) — closing the position "
                "immediately rather than leaving it unprotected", self.network.upper(), sym, exc)
            self._emergency_close(sym)
            self.last_fill = None
            return False

        logger.info("[{}] SL={:.2f} TP={:.2f} attached for {}",
                    self.network.upper(), intent.stop_loss, intent.take_profit_1, sym)
        return True

    def _rehearse_open(self, intent: TradeIntent, open_args: list[str], sym: str,
                       side: str, size: str, leverage: int) -> bool:
        """Dry-run entry: the venue validates and prices the order, nothing is
        signed. ``mm perps open --dry-run`` returns ``{"status": "prepared",
        "averagePrice", "filledSize"}`` (captured live); ``mm perps quote``
        adds the venue's own liquidation estimate, which is checked against
        the stop so the rehearsal also tests the risk layer's assumption."""
        try:
            prepared = self.perps(*open_args, "--dry-run")
        except MMError as exc:
            logger.error("[{}] venue would REJECT {} {} size={}: {}",
                         self.tag, side, sym, size, exc)
            return False

        quote_args = ["quote", "--symbol", sym, "--side", side, "--size", size,
                      "--leverage", str(int(leverage))]
        if self.settings.perps_order_type == "limit":
            quote_args += ["--type", "limit", "--limit-px", _format_price(intent.entry_price)]
        try:
            quote = self.perps(*quote_args)
            liq = as_float(quote.get("estimatedLiquidationPrice"))
            beyond = liq > 0 and ((side == "long" and liq >= intent.stop_loss)
                                  or (side == "short" and liq <= intent.stop_loss))
            if beyond:
                logger.error("[{}] venue estimates liquidation at {:.2f}, before the stop "
                             "at {:.2f} — a live run would be liquidated first",
                             self.tag, liq, intent.stop_loss)
                return False
            logger.info("[{}] quote: notional={} fee={} liquidation={}", self.tag,
                        quote.get("notional", "?"), quote.get("estimatedFee", "?"),
                        quote.get("estimatedLiquidationPrice", "?"))
        except MMError as exc:
            logger.warning("[{}] quote unavailable for {}: {}", self.tag, sym, exc)

        fill = _fill_from(prepared, "averagePrice", "filledSize")
        self.last_fill = fill or Fill(price=intent.entry_price, size=float(size))
        logger.info("[{}] WOULD OPEN {} {} size={} lev={}x at {:.2f} SL={:.2f} TP={:.2f} "
                    "— nothing signed", self.tag, side, sym, size, leverage,
                    self.last_fill.price, intent.stop_loss, intent.take_profit_1)
        return True

    def _await_entry(self, sym: str, order_id: Any, requested: float,
                     planned_price: float) -> Fill | None:
        """Wait for an entry that did not fill immediately, then make sure
        nothing is left resting.

        Polls positions for up to ``perps_limit_fill_timeout_s``. A full fill
        returns at once. Otherwise the remainder is cancelled and positions
        are read one last time — the order may have filled, wholly or in
        part, while the cancel was in flight — so whatever is open is
        returned and gets its SL/TP like any other entry. ``None`` means no
        position exists and the caller must not record one.
        """
        deadline = time.monotonic() + max(self.settings.perps_limit_fill_timeout_s, 0.0)
        logger.info("[{}] {} entry is resting (order {}) — waiting up to {:.0f}s for a fill",
                    self.network.upper(), sym, order_id or "?",
                    self.settings.perps_limit_fill_timeout_s)
        while True:
            fill = self._position_fill(sym, planned_price)
            if fill is not None and fill.size >= requested * 0.999:
                return fill
            if time.monotonic() >= deadline:
                break
            time.sleep(max(self.settings.perps_fill_poll_s, 0.0))

        cancelled = self._cancel_entry(sym, order_id, planned_price)
        fill = self._position_fill(sym, planned_price)
        if fill is not None:
            logger.warning("[{}] {} entry filled {:.6g} of {:.6g} before the remainder "
                           "was cancelled — protecting the partial position",
                           self.network.upper(), sym, fill.size, requested)
            return fill
        if cancelled:
            logger.info("[{}] {} entry did not fill; order cancelled, no position opened",
                        self.network.upper(), sym)
        else:
            logger.critical(
                "[{}] could not cancel the unfilled {} entry (order {}). It may still be "
                "RESTING, and if it fills it will have NO STOP. Cancel it now with "
                "`mm perps cancel --order-id {} --network {}` or in the Hyperliquid UI.",
                self.network.upper(), sym, order_id or "?", order_id or "<id>", self.network)
        return None

    def _position_fill(self, sym: str, planned_price: float) -> Fill | None:
        """The open position for ``sym`` as a :class:`Fill`, or ``None``."""
        try:
            entry = self.fetch_position(sym)
        except MMError as exc:
            logger.warning("could not read positions while waiting for {}: {}", sym, exc)
            return None
        if entry is None:
            return None
        size = abs(as_float(pick(entry, "size", "szi", "sz", default=0.0)))
        price = as_float(pick(entry, "entryPrice", "entryPx", default=0.0)) or planned_price
        return Fill(price=price, size=size) if size > 0 else None

    def _cancel_entry(self, sym: str, order_id: Any, planned_price: float) -> bool:
        """Cancel the resting entry. Without an order id, cancel only resting
        orders on ``sym`` at the entry's own limit price, so nothing else on
        the book is touched."""
        ids = [str(order_id)] if order_id else []
        if not ids:
            try:
                orders = as_list(self.perps("orders"), "orders")
            except MMError as exc:
                logger.error("could not list orders to cancel the {} entry: {}", sym, exc)
                return False
            for o in orders:
                if (isinstance(o, dict) and to_hl_symbol(str(o.get("symbol", ""))) == sym
                        and abs(as_float(o.get("limitPrice")) - planned_price)
                        <= planned_price * 1e-6):
                    ids.append(str(o.get("orderId")))
            if not ids:
                return True  # nothing resting: it filled or never rested
        ok = True
        for oid in ids:
            try:
                self.perps("cancel", "--order-id", oid, "--symbol", sym, "--yes", signing=True)
            except MMError as exc:
                logger.error("[{}] cancel of {} order {} failed: {}",
                             self.network.upper(), sym, oid, exc)
                ok = False
        return ok

    def _emergency_close(self, sym: str) -> None:
        try:
            # The exit leg needs its slippage cap too — it is the one MetaMask
            # warns is easiest to forget, and an emergency close is exactly
            # when the book may be thin.
            self.perps("close", "--symbol", sym,
                       "--max-slippage-bps", str(int(self.settings.perps_max_slippage_bps)),
                       "--yes", signing=True)
            logger.warning("[{}] unwound unprotected {} position", self.network.upper(), sym)
        except MMError as close_exc:
            logger.critical(
                "[{}] COULD NOT UNWIND {} — there may be a LIVE POSITION WITH NO "
                "STOP on Hyperliquid. Intervene manually now: {}",
                self.network.upper(), sym, close_exc)

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        sym = to_hl_symbol(pos.symbol)
        if self.dry_run:
            # No position exists on the venue, so `close --dry-run` has
            # nothing to validate against.
            logger.info("[{}] WOULD CLOSE {} @ {:.2f} ({}) pnl={:.2f}", self.tag, sym,
                        price, reason, pos.pnl_at(price))
            return
        try:
            self.perps("close", "--symbol", sym,
                        "--max-slippage-bps", str(int(self.settings.perps_max_slippage_bps)),
                        "--yes", signing=True)
            # The close is a market order, so the real fill will differ from
            # the engine's assumed `price`. The PositionMonitor is what
            # reconciles the true fill; this log makes the gap visible.
            logger.info("[{}] market close {} ({}) — engine assumed {:.2f}, "
                        "actual fill may differ", self.network.upper(), sym, reason, price)
        except MMError as exc:
            # Reaching here after the venue already filled the SL/TP is
            # expected and harmless (nothing left to close) — but it is not
            # distinguishable from a real failure without a positions call,
            # so it is reported rather than swallowed.
            logger.error("[{}] close failed for {} ({}) — verify with "
                         "`mm perps positions`: {}", self.network.upper(), sym, reason, exc)

    def update_stop(self, pos: Position, new_stop: float) -> bool:
        """Move the venue-side stop (breakeven move, spec 8.3).

        Returns ``False`` on failure so the engine leaves ``pos.stop_loss``
        untouched — a local stop that disagrees with the venue is worse than
        no move at all.
        """
        sym = to_hl_symbol(pos.symbol)
        if self.dry_run:
            logger.info("[{}] WOULD MOVE stop to {:.2f} for {}", self.tag, new_stop, sym)
            return True
        try:
            self.perps("modify", "--symbol", sym, "--sl", _format_price(new_stop), "--yes",
                       signing=True)
            logger.info("[{}] stop moved to {:.2f} for {}",
                        self.network.upper(), new_stop, sym)
            return True
        except MMError as exc:
            logger.error("[{}] stop update failed — the ORIGINAL stop is likely still "
                         "live at {:.2f}: {}", self.network.upper(), pos.stop_loss, exc)
            return False

    # -- read-only helpers ------------------------------------------------ #
    def fetch_position(self, symbol: str) -> dict[str, Any] | None:
        """The open position for ``symbol``, or ``None`` if flat.

        Inner payload shape is unverified (see module docstring) — key
        lookups are tolerant and a miss degrades to ``None``/``0.0`` rather
        than raising.
        """
        sym = to_hl_symbol(symbol)
        data = self.perps("positions")
        for entry in as_list(data, "positions"):
            if not isinstance(entry, dict):
                continue
            coin = str(pick(entry, "symbol", "coin", "asset", "market", default="")).upper()
            if to_hl_symbol(coin) != sym:
                continue
            if abs(as_float(pick(entry, "size", "szi", "sz", "contracts", default=0.0))) > 0:
                return entry
        return None

    def fetch_collateral_usd(self) -> float:
        """Account equity on the venue, used to re-seed Maya's equity at startup.

        Reads ``totalBalance`` — the account's total value — as confirmed
        against a live ``mm perps balance --network testnet`` response::

            {"venue": "hyperliquid", "totalBalance": "0",
             "spendableBalance": "0.0", "withdrawableBalance": "0.0",
             "marginUsed": "0", "unrealizedPnl": "0.00",
             "returnOnEquity": "0", "subAccounts": {"main": {...}}}

        Every figure arrives as a *string*, hence :func:`as_float`.
        ``spendableBalance`` is free margin, not equity, so it is only a
        fallback: seeding Maya's risk manager off free margin would understate
        equity whenever a position is open.
        """
        data = self.perps("balance")
        value = pick(data, "totalBalance", "accountValue", "equity", default=None)
        if value is None:
            # Nested form, if the venue ever reports only per-sub-account.
            sub = data.get("subAccounts")
            if isinstance(sub, dict) and isinstance(sub.get("main"), dict):
                value = pick(sub["main"], "totalBalance", "accountValue", default=None)
        if value is None:
            value = pick(data, "spendableBalance", "withdrawableBalance", default=0.0)
            logger.warning(
                "`mm perps balance` had no totalBalance field — falling back to "
                "free margin, which understates equity while a position is open. "
                "Payload keys: {}", sorted(data))
        return as_float(value)

    def fetch_balance_detail(self) -> dict[str, float]:
        """Full balance breakdown, for diagnostics rather than the hot path."""
        data = self.perps("balance")
        return {
            "total": as_float(pick(data, "totalBalance", default=0.0)),
            "spendable": as_float(pick(data, "spendableBalance", default=0.0)),
            "withdrawable": as_float(pick(data, "withdrawableBalance", default=0.0)),
            "margin_used": as_float(pick(data, "marginUsed", default=0.0)),
            "unrealized_pnl": as_float(pick(data, "unrealizedPnl", default=0.0)),
        }


# -- payload helpers ------------------------------------------------------- #
def _describe_error(err: Any) -> str:
    """``CODE: message (hint: ...)`` from a CLI error, whatever its shape."""
    if isinstance(err, dict) and "error" in err and "ok" in err:
        err = err["error"]
    if not isinstance(err, dict):
        return str(err)
    message = str(pick(err, "message", "msg", "reason", default=err))
    code = err.get("code")
    hint = err.get("hint")
    text = f"{code}: {message}" if code else message
    if hint and hint not in message:
        text += f" (hint: {hint})"
    return text


def _error_code(err: Any) -> str | None:
    return str(err["code"]) if isinstance(err, dict) and err.get("code") else None


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present, non-``None`` value among ``keys``."""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def as_list(data: Any, *keys: str) -> list[Any]:
    """Coerce a payload to a list of entries, tolerating a few wrappings."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        value = data.get("result")
        if isinstance(value, list):
            return value
    return []


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fill_from(data: dict[str, Any], price_key: str, size_key: str) -> Fill | None:
    price = as_float(data.get(price_key))
    size = abs(as_float(data.get(size_key)))
    return Fill(price=price, size=size) if price > 0 and size > 0 else None


def _format_size(qty: float, decimals: int) -> str:
    """Round a size down to the venue's size precision.

    Hyperliquid rejects orders with more size decimals than the market's
    ``szDecimals``. Rounding *down* keeps the order inside the risk-approved
    quantity rather than nudging it above.
    """
    factor = 10 ** decimals
    truncated = int(abs(qty) * factor) / factor
    return f"{truncated:.{decimals}f}"


def _format_size_up(qty: float, decimals: int) -> str:
    """Round a size *up* to the venue's precision (minimum-size mode only)."""
    factor = 10 ** decimals
    steps = math.ceil(round(abs(qty) * factor, 9))
    return f"{steps / factor:.{decimals}f}"


def _format_price(price: float) -> str:
    # Trim trailing zeros so prices read as "66800" not "66800.00000".
    return f"{price:.6f}".rstrip("0").rstrip(".")
