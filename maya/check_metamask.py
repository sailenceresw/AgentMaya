"""Layer 1 / 8 — MetaMask Agent Wallet + Hyperliquid connectivity diagnostics.

The Hyperliquid counterpart to :mod:`maya.check_connection`. Same contract:
**read-only**, never places or cancels anything, and returns the same
:class:`~maya.check_connection.ConnectionReport` so :mod:`maya.run` can apply
one startup gate regardless of venue.

Run directly:

    MAYA_MODE=testnet MAYA_VENUE=hyperliquid python -m maya.check_metamask

What it proves before any order is placed:

1. the ``mm`` CLI is installed and on PATH,
2. the CLI session is authenticated (``mm auth status``),
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

    # 2. Authentication. Everything downstream is signed, so a failure here
    #    makes the rest meaningless — bail immediately.
    try:
        auth = broker.auth_status()
    except MMError as exc:
        report.errors.append(f"`mm auth status` failed: {exc}")
        report.ok = False
        return report

    if not bool(pick(auth, "authenticated", "isAuthenticated", default=False)):
        reason = pick(auth, "reason", "message", default="not signed in")
        report.errors.append(f"MetaMask Agent Wallet is not authenticated ({reason}) — run `mm login`")
        report.ok = False
        return report

    # 3. Collateral. A zero balance is the single most common reason a first
    #    Hyperliquid order is rejected, so it is called out explicitly.
    try:
        report.usdt_balance = broker.fetch_collateral_usd()
        report.usdt_available = report.usdt_balance
        if (report.usdt_balance or 0) <= 0:
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
        markets = broker.perps("markets", "--symbol", hl_symbol)
        for entry in as_list(markets, "markets"):
            if not isinstance(entry, dict):
                continue
            name = str(pick(entry, "symbol", "coin", "name", default="")).upper()
            if to_hl_symbol(name) != hl_symbol:
                continue
            price = as_float(pick(entry, "markPx", "markPrice", "price", "oraclePx",
                                    default=0.0))
            if price > 0:
                report.last_price = price
            break
        if report.last_price is None:
            report.warnings.append(
                f"could not read a mark price for {hl_symbol} from `mm perps markets` "
                "— confirm the symbol exists on this venue"
            )
    except MMError as exc:
        report.warnings.append(f"could not fetch market data for {hl_symbol}: {exc}")

    # 5. Spec 8.4 reconciliation inputs. Best-effort: these must not stop the
    #    bot starting when the rest is healthy, but anything found is escalated
    #    by the caller in maya.run.
    try:
        positions = broker.perps("positions")
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
