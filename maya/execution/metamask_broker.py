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
so those are accurate. Against a live authenticated session on Hyperliquid
testnet:

* ``perps markets`` parsing is **confirmed** — a real mark price (64773.00 for
  BTC) was extracted through the tolerant key lookup below.
* ``perps balance`` and ``perps positions`` parsing is **still unproven**. Both
  were exercised, but against an *empty* sub-account, where a correct parse and
  a failed key lookup are indistinguishable: :func:`as_float` returns ``0.0``
  and :func:`as_list` returns ``[]`` in both cases. Confirming these needs a
  funded account with an open position.
* No order has ever been placed through this module.

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
import os
import shutil
import subprocess
import time
from typing import Any

from loguru import logger

from maya.config import Settings
from maya.execution.models import Position
from maya.strategy.models import TradeIntent

# Quote suffixes stripped when mapping a Maya symbol to a Hyperliquid one.
# Maya uses Binance-style "BTCUSDT"; Hyperliquid markets are bare coins ("BTC").
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "PERP")


class MMError(RuntimeError):
    """A ``mm`` invocation failed, timed out, or returned ``ok: false``."""


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
        self.timeout_s = settings.mm_timeout_s

    # -- transport -------------------------------------------------------- #
    def _run(self, *args: str, timeout: float | None = None) -> dict[str, Any]:
        """Invoke ``mm`` and return the parsed ``data`` payload.

        Raises :class:`MMError` on non-zero exit, unparseable output, a
        timeout, or an ``ok: false`` envelope.
        """
        cmd = [self.binary, *args, "--json"]
        printable = " ".join(["mm", *args, "--json"])
        limit = timeout if timeout is not None else self.timeout_s
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=limit, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A timeout is genuinely ambiguous: the order may or may not have
            # been submitted. Say so loudly rather than implying it failed.
            raise MMError(
                f"`{printable}` timed out after {limit}s — the order state "
                f"is UNKNOWN; check `mm perps positions` before retrying"
            ) from exc

        # This CLI is slow enough to matter on a 1m timeframe (measured 24-51s
        # for a trivial `auth status`), so every call's cost is logged rather
        # than hidden. If these numbers are large, the strategy timeframe —
        # not the timeout — is what needs revisiting.
        elapsed = time.monotonic() - started
        if elapsed > 10:
            logger.warning("`{}` took {:.1f}s — slow enough to affect entry quality "
                           "on a {} timeframe", printable, elapsed, self.settings.timeframe)
        else:
            logger.debug("`{}` took {:.1f}s", printable, elapsed)

        stdout = (proc.stdout or "").strip()
        if not stdout:
            raise MMError(
                f"`{printable}` produced no output (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()[:400]}"
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise MMError(f"`{printable}` returned non-JSON output: {stdout[:400]}") from exc

        if not payload.get("ok", False):
            raise MMError(f"`{printable}` failed: {_describe_error(payload)}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {"result": data}

    def perps(self, *args: str, timeout: float | None = None) -> dict[str, Any]:
        """``mm perps <args>`` with the venue/network flags this broker is bound to."""
        return self._run(
            "perps", *args,
            "--venue", self.venue,
            "--network", self.network,
            timeout=timeout,
        )

    def auth_status(self) -> dict[str, Any]:
        """``mm auth status`` — whether the CLI session can sign anything."""
        return self._run("auth", "status")

    # -- Broker protocol -------------------------------------------------- #
    def open_position(self, intent: TradeIntent, qty: float, leverage: int) -> bool:
        sym = to_hl_symbol(intent.symbol)
        side = "long" if intent.direction == "long" else "short"
        size = _format_size(qty, self.settings.perps_size_decimals)

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
            # planned risk honest. Carries the same caveat as the Binance
            # path — an unfilled limit still creates a local Position.
            open_args += ["--type", "limit", "--limit-px", _format_price(intent.entry_price)]
        else:
            open_args += ["--type", "market",
                          "--max-slippage-bps", str(int(self.settings.perps_max_slippage_bps))]

        try:
            self.perps(*open_args, "--yes")
        except MMError as exc:
            logger.error("[{}] hyperliquid open failed for {}: {}",
                         self.network.upper(), sym, exc)
            return False

        logger.info("[{}] OPEN {} {} size={} lev={}x entry={:.2f}",
                    self.network.upper(), side, sym, size, leverage, intent.entry_price)

        # Attach SL/TP. This is a separate round-trip and the position is
        # unprotected until it lands — if it fails, unwind immediately.
        try:
            self.perps(
                "modify",
                "--symbol", sym,
                "--sl", _format_price(intent.stop_loss),
                "--tp", _format_price(intent.take_profit_1),
                "--yes",
            )
        except MMError as exc:
            logger.error(
                "[{}] SL/TP attach FAILED for {} ({}) — closing the position "
                "immediately rather than leaving it unprotected", self.network.upper(), sym, exc)
            try:
                self.perps("close", "--symbol", sym, "--yes")
                logger.warning("[{}] unwound unprotected {} position", self.network.upper(), sym)
            except MMError as close_exc:
                logger.critical(
                    "[{}] COULD NOT UNWIND {} — there may be a LIVE POSITION WITH NO "
                    "STOP on Hyperliquid. Intervene manually now: {}",
                    self.network.upper(), sym, close_exc)
            return False

        logger.info("[{}] SL={:.2f} TP={:.2f} attached for {}",
                    self.network.upper(), intent.stop_loss, intent.take_profit_1, sym)
        return True

    def close_position(self, pos: Position, price: float, reason: str) -> None:
        sym = to_hl_symbol(pos.symbol)
        try:
            self.perps("close", "--symbol", sym,
                        "--max-slippage-bps", str(int(self.settings.perps_max_slippage_bps)),
                        "--yes")
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
        try:
            self.perps("modify", "--symbol", sym, "--sl", _format_price(new_stop), "--yes")
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
        """Account equity on the venue, used to re-seed Maya's equity at startup."""
        data = self.perps("balance")
        return as_float(pick(
            data, "accountValue", "equity", "totalEquity", "withdrawable",
            "available", "balance", default=0.0,
        ))


# -- payload helpers ------------------------------------------------------- #
def _describe_error(payload: dict[str, Any]) -> str:
    err = payload.get("error", payload)
    if isinstance(err, dict):
        return str(pick(err, "message", "msg", "reason", "code", default=err))
    return str(err)


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


def _format_size(qty: float, decimals: int) -> str:
    """Round a size down to the venue's size precision.

    Hyperliquid rejects orders with more size decimals than the market's
    ``szDecimals``. Rounding *down* keeps the order inside the risk-approved
    quantity rather than nudging it above.
    """
    factor = 10 ** decimals
    truncated = int(abs(qty) * factor) / factor
    return f"{truncated:.{decimals}f}"


def _format_price(price: float) -> str:
    # Trim trailing zeros so prices read as "66800" not "66800.00000".
    return f"{price:.6f}".rstrip("0").rstrip(".")
