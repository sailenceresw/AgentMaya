"""Layer 1 / 8 — MetaMask Agent Wallet + Hyperliquid connectivity diagnostics.

The Hyperliquid counterpart to :mod:`maya.check_connection`. Same contract:
**read-only**, never places or cancels anything, and returns the same
:class:`~maya.check_connection.ConnectionReport` so :mod:`maya.run` can apply
one startup gate regardless of venue.

Run directly:

    MAYA_MODE=testnet MAYA_VENUE=hyperliquid python -m maya.check_metamask

What it proves before any order is placed:

1. the ``mm`` CLI is installed and on PATH, and is a version this broker was
   verified against (``mm doctor``),
2. the CLI session is authenticated and initialised with a wallet
   (``mm doctor``; ``mm auth status`` on CLIs that predate it),
3. the Hyperliquid sub-account has collateral — it is empty on first use and
   orders are rejected until ``mm perps deposit`` has been run,
4. the venue shows no position/order that Maya has no local record of
   (spec 8.4 — a fresh process has zero local state, so anything here is a
   mismatch a human must resolve).

There is no clock-skew check: unlike Binance's signed REST API, order
authorization here is a wallet signature, so local clock drift is not a
rejection cause.
"""
from __future__ import annotations

import sys

from loguru import logger

from maya.check_connection import ConnectionReport
from maya.config import Settings, Venue, get_settings
from maya.execution.metamask_broker import (
    MMError,
    MetaMaskPerpsBroker,
    as_float,
    as_list,
    pick,
    to_hl_symbol,
)

# The CLI major version this module's command surface and output parsing
# were verified against (v7.1.0).
MIN_CLI_MAJOR = 7


def _major(version: str) -> int | None:
    try:
        return int(version.lstrip("v").split(".", 1)[0])
    except ValueError:
        return None


def run_diagnostics(settings: Settings | None = None) -> ConnectionReport:
    """Collect every safe check into a report. Never raises for ordinary
    auth/connectivity failures — the caller decides what is fatal."""
    settings = settings or get_settings()
    report = ConnectionReport(ok=True, mode=settings.mode.value, symbol=settings.symbol)

    if settings.venue is not Venue.HYPERLIQUID:
        report.errors.append(
            f"venue={settings.venue.value} is not hyperliquid — use "
            "`python -m maya.check_connection` for the Binance path"
        )
        report.ok = False
        return report

    # 1. CLI present + constructible.
    try:
        broker = MetaMaskPerpsBroker(settings)
    except MMError as exc:
        report.errors.append(str(exc))
        report.ok = False
        return report

    logger.info("Targeting Hyperliquid {} via MetaMask Agent Wallet",
                broker.network.upper())

    # 2. CLI health. `mm doctor` needs no session and reports the CLI
    #    version plus whether it is signed in *and* initialised with a wallet
    #    — MetaMask's docs say to confirm both before any wallet operation.
    #    Older CLIs without it fall back to `mm auth status`.
    try:
        doctor = broker.doctor()
    except MMError as exc:
        logger.debug("`mm doctor` unavailable ({}); falling back to `mm auth status`", exc)
        doctor = None

    if doctor is not None:
        version = str(doctor.get("cli", "?"))
        logger.info("MetaMask Agent Wallet CLI v{}", version)
        if _major(version) is not None and _major(version) < MIN_CLI_MAJOR:
            report.warnings.append(
                f"mm CLI v{version} is older than v{MIN_CLI_MAJOR}, which this broker was "
                "verified against — upgrade with `npm install -g @metamask/agent-wallet@latest`")
        if not doctor.get("authenticated"):
            report.errors.append("MetaMask Agent Wallet is not authenticated — run `mm login`")
            report.ok = False
            return report
        if doctor.get("initialized") is False:
            report.errors.append(
                "MetaMask Agent Wallet is signed in but not initialised — run "
                "`mm init --wallet server-wallet --mode guard`")
            report.ok = False
            return report
    else:
        # Everything downstream is signed, so a failure here makes the rest
        # meaningless — bail immediately.
        try:
            auth = broker.auth_status()
        except MMError as exc:
            report.errors.append(f"`mm auth status` failed: {exc}")
            report.ok = False
            return report

        if not bool(pick(auth, "authenticated", "isAuthenticated", default=False)):
            reason = pick(auth, "reason", "message", default="not signed in")
            report.errors.append(
                f"MetaMask Agent Wallet is not authenticated ({reason}) — run `mm login`")
            report.ok = False
            return report

    # Trading mode, informational. Guard Mode can pause a deposit or
    # withdrawal for phone approval; Beast Mode never does. Order placement
    # itself happens on Hyperliquid and is not gated by either.
    expected = settings.mm_expected_address.strip().lower()
    try:
        info = broker.wallet_info()
    except MMError as exc:
        logger.debug("could not read wallet trading mode: {}", exc)
        info = {"mode": None, "address": None}
    mode, address = info["mode"], info["address"]
    if address:
        logger.info("Agent wallet        : {}", address)
    if mode:
        logger.info("Wallet trading mode : {}", mode)
        if mode.lower() == "beast" and broker.network == "mainnet":
            report.warnings.append(
                "wallet is in Beast Mode on mainnet: outflows skip the Guard Mode "
                "approval and 24h limit. Run `mm wallet trading-mode set guard` "
                "unless that is deliberate.")
    # Google, email and MetaMask Mobile QR sign-ins each resolve to a different
    # wallet, so signing in "the other way" silently points Maya at another
    # account — usually an empty one, possibly one holding something else.
    if expected:
        if not address:
            report.errors.append(
                "MAYA_MM_EXPECTED_ADDRESS is set but the active wallet address could "
                "not be read — refusing to trade from an unverified wallet")
            report.ok = False
            return report
        if address.lower() != expected:
            report.errors.append(
                f"active agent wallet {address} is not MAYA_MM_EXPECTED_ADDRESS "
                f"{settings.mm_expected_address}. Each sign-in method (Google, email, "
                "Mobile QR) resolves to a different wallet — log out and sign in the "
                "way you originally did.")
            report.ok = False
            return report
    elif address:
        report.warnings.append(
            f"set MAYA_MM_EXPECTED_ADDRESS={address} to pin this wallet: a different "
            "sign-in method would silently switch Maya to another wallet")

    # 3. Collateral. A zero balance is the single most common reason a first
    #    Hyperliquid order is rejected, so it is called out explicitly.
    try:
        report.usdt_balance = broker.fetch_collateral_usd()
        report.usdt_available = report.usdt_balance
        if (report.usdt_balance or 0) <= 0 and settings.perps_dry_run:
            logger.info("Collateral is 0 — fine for a dry run, which never submits")
        elif (report.usdt_balance or 0) <= 0 and broker.network == "testnet":
            report.warnings.append(
                "Hyperliquid testnet collateral is 0, and per MetaMask testnet "
                "\"can't currently be funded through Agent Wallet's funding path\". "
                "To rehearse, run against mainnet with nothing signed: "
                "MAYA_MODE=live MAYA_PERPS_DRY_RUN=true."
            )
        elif (report.usdt_balance or 0) <= 0:
            report.warnings.append(
                "Hyperliquid collateral is 0 — the sub-account is empty on first "
                f"use. Run `mm perps deposit --amount <USDC> --network {broker.network}` "
                "before expecting any order to fill."
            )
        elif report.usdt_balance < 100:
            report.warnings.append(
                f"collateral ${report.usdt_balance:.2f} is below the ~$100 minimum "
                "viable size for the fee math to work cleanly"
            )
    except MMError as exc:
        report.errors.append(f"`mm perps balance` failed: {exc}")
        report.ok = False

    # 4. Mark price for the traded symbol — best-effort. The payload shape of
    #    `mm perps markets` is unverified, so a miss is a warning, not a
    #    failure: it is informational, nothing downstream depends on it.
    hl_symbol = to_hl_symbol(settings.symbol)
    try:
        # Through the broker's cache, so sizing the fee quote below does not
        # fetch the same market metadata a second time.
        entry = broker.market_meta(hl_symbol)
        price = as_float(pick(entry, "markPx", "markPrice", "price", "oraclePx",
                              default=0.0))
        if price > 0:
            report.last_price = price
        else:
            report.warnings.append(
                f"could not read a mark price for {hl_symbol} from `mm perps markets` "
                "— confirm the symbol exists on this venue"
            )
    except MMError as exc:
        report.warnings.append(f"could not fetch market data for {hl_symbol}: {exc}")

    # 4b. Fees, from MetaMask itself. `mm perps quote` reports the venue
    #     rate at this wallet's tier (`feeRate`: maker for a limit quote,
    #     taker for a market one) and MetaMask's builder fee
    #     (`builderFeeBips`, in basis points). Best-effort: a miss leaves
    #     the conservative defaults in place.
    if report.last_price:
        report.fee_calibration = _read_fees(broker, hl_symbol, report.last_price,
                                            settings.leverage)

    # 5. Spec 8.4 reconciliation inputs. Best-effort: these must not stop the
    #    bot starting when the rest is healthy, but anything found is escalated
    #    by the caller in maya.run.
    try:
        # --all-dexes: reads default to the main DEX only, so a position left
        # on a HIP-3 DEX would otherwise be invisible to this check.
        positions = broker.perps("positions", "--all-dexes")
        for entry in as_list(positions, "positions"):
            if not isinstance(entry, dict):
                continue
            size = as_float(pick(entry, "size", "szi", "sz", "contracts", default=0.0))
            if abs(size) > 0:
                report.open_positions.append({
                    "symbol": pick(entry, "symbol", "coin", "asset", default="?"),
                    "contracts": size,
                    "side": pick(entry, "side", "direction",
                                  default="long" if size > 0 else "short"),
                })
    except MMError as exc:
        report.warnings.append(f"could not fetch open positions: {exc}")

    try:
        orders = broker.perps("orders")
        for entry in as_list(orders, "orders"):
            if isinstance(entry, dict):
                report.open_orders.append({
                    "id": pick(entry, "oid", "orderId", "id", default="?"),
                    "type": pick(entry, "orderType", "type", default="?"),
                    "side": pick(entry, "side", "direction", default="?"),
                })
    except MMError as exc:
        report.warnings.append(f"could not fetch open orders: {exc}")

    return report


def _read_fees(broker: MetaMaskPerpsBroker, symbol: str, mark: float,
               leverage: int) -> dict[str, float]:
    """Maker, taker and builder fee in bps, as quoted for this wallet."""
    from maya.config import HYPERLIQUID_MIN_NOTIONAL_USD, MIN_SIZE_HEADROOM
    from maya.execution.metamask_broker import _format_price, _format_size_up

    size = _format_size_up(HYPERLIQUID_MIN_NOTIONAL_USD * MIN_SIZE_HEADROOM / mark,
                           broker.size_decimals_for(symbol))
    base = ["quote", "--symbol", symbol, "--side", "long", "--size", size,
            "--leverage", str(int(leverage))]
    out: dict[str, float] = {}
    for key, extra in (("fee_maker_bps", ["--type", "limit", "--limit-px", _format_price(mark)]),
                       ("fee_taker_bps", ["--type", "market"])):
        try:
            quote = broker.perps(*base, *extra)
        except MMError as exc:
            logger.warning("fee quote ({}) unavailable: {}", key, exc)
            continue
        rate = quote.get("feeRate")
        if rate is not None:
            out[key] = as_float(rate) * 1e4
        # Only an explicit figure lowers the builder-fee assumption; a quote
        # without the field leaves the 10bps cap in place.
        if quote.get("builderFeeBips") is not None:
            out["builder_fee_bps"] = as_float(quote["builderFeeBips"])
    if out:
        logger.info("Fees quoted by MetaMask: {}", ", ".join(
            f"{k.replace('_bps', '')}={v:.2f}bps" for k, v in sorted(out.items())))
    return out


def _print_report(report: ConnectionReport) -> None:
    logger.info("=== Maya — MetaMask / Hyperliquid Check ({}) ===", report.mode.upper())
    logger.info("Symbol            : {} (as {})", report.symbol, to_hl_symbol(report.symbol))
    if report.last_price is not None:
        logger.info("Mark price        : {:.2f}", report.last_price)
    if report.usdt_balance is not None:
        logger.info("Collateral        : ${:.2f}", report.usdt_balance)
    if report.open_positions:
        logger.warning("Open positions on venue: {}", report.open_positions)
    elif report.usdt_balance is not None:
        logger.info("Open positions    : none")
    if report.open_orders:
        logger.warning("Open orders on venue: {}", report.open_orders)
    elif report.usdt_balance is not None:
        logger.info("Open orders       : none")
    for w in report.warnings:
        logger.warning(w)
    for e in report.errors:
        logger.error(e)
    logger.info("Result            : {}", "OK" if report.ok else "FAILED")


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    report = run_diagnostics(get_settings())
    _print_report(report)
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
