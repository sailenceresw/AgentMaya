"""Hyperliquid (SDK) connection check and order smoke test.

    python -m maya.check_hyperliquid            # read-only report
    python -m maya.check_hyperliquid --smoke    # place a far limit, cancel it

Returns the same :class:`~maya.check_connection.ConnectionReport` the other
preflights do, so :mod:`maya.run` applies the same startup rules. Before
that, any position found without a stop is closed (see
``HyperliquidSDKBroker.close_unprotected_positions``).

The smoke test places one buy limit at half the current mid price, which
cannot fill, sized at the venue minimum, then cancels it and confirms no
order is left. Testnet only unless the mainnet flags are set.
"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

from maya.check_connection import ConnectionReport
from maya.config import HYPERLIQUID_MIN_NOTIONAL_USD, MIN_SIZE_HEADROOM, Settings, get_settings
from maya.execution.hyperliquid_broker import (
    MAINNET_URL,
    HyperliquidSDKBroker,
    base_url,
    order_statuses,
    round_price,
)


def unified_usdc(info, address: str) -> tuple[float, float] | None:
    """``(total, free)`` spot USDC when the account is a unified account, else None.

    A unified account keeps its perp collateral as *spot* USDC, so the perp
    ``marginSummary.accountValue`` reads 0.0 on a funded, flat account. Seen
    on testnet (Nov 2026): ``userAbstraction`` returned ``"unifiedAccount"``,
    perp account value ``"0.0"``, spot ``{"coin": "USDC", "total": "999.0",
    "hold": "0.0"}``. Reading only the perp side reported that account as
    unfunded and left equity at the config default.

    With a position open the sum overstates: testnet (Nov 2026) reported
    $1,069.86 with a 0.02187 BTC position on $68 margin, and spot USDC alone
    was $966.44 after a close that cost $36, so the position's perp value was
    counted on top of spot that already held it. Acceptable here, because
    startup refuses to run with an open position and reads this while flat.

    Any failure returns None and the caller keeps the perp figure.
    """
    try:
        if info.query_user_abstraction_state(address) != "unifiedAccount":
            return None
        for bal in info.spot_user_state(address).get("balances", []):
            if bal.get("coin") == "USDC":
                total = float(bal.get("total", 0) or 0)
                return total, total - float(bal.get("hold", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - a balance report must not stop the preflight
        logger.debug("unified-account balance read skipped: {}", exc)
    return None


def agent_problem(broker: HyperliquidSDKBroker, network: str) -> str | None:
    """Why the signing key cannot act for the account, or None if it can.

    Seen on testnet (Nov 2026): reads worked, but every order and close was
    refused with "User or API Wallet 0x... does not exist." because the API
    wallet was not (or no longer) approved; ``userRole`` returned
    ``{"role": "missing"}``. Without this check the preflight passed and the
    failure only showed when a position needed closing.

    A query failure returns None: an unreachable role endpoint must not stop
    a key that works, and a key that does not work is still refused by the venue.
    """
    wallet = getattr(broker.exchange, "wallet", None)
    agent = getattr(wallet, "address", None)
    if not agent or agent.lower() == broker.address.lower():
        return None                               # main key signs for itself
    try:
        role = broker.info.user_role(agent)
    except Exception as exc:  # noqa: BLE001
        logger.debug("API wallet role check skipped: {}", exc)
        return None
    if isinstance(role, dict) and role.get("role") == "agent" and \
            str((role.get("data") or {}).get("user", "")).lower() == broker.address.lower():
        return None
    app = "app.hyperliquid.xyz" if network == "MAINNET" else "app.hyperliquid-testnet.xyz"
    return (f"API wallet {agent} is not approved for account {broker.address} on {network} "
            f"(venue says {role}); it can read but not trade or close. Approve a new API "
            f"wallet at {app}/API and put its key in HL_API_WALLET_KEY.")


def run_diagnostics(settings: Settings | None = None,
                    broker: HyperliquidSDKBroker | None = None) -> ConnectionReport:
    settings = settings or get_settings()
    report = ConnectionReport(ok=True, mode=settings.mode.value, symbol=settings.symbol)
    try:
        broker = broker or HyperliquidSDKBroker(settings)
    except Exception as exc:  # noqa: BLE001 - every failure here is a report line
        report.ok = False
        report.errors.append(f"could not connect to Hyperliquid: {exc}")
        return report

    network = "MAINNET" if base_url(settings) == MAINNET_URL else "testnet"
    if network == "MAINNET":
        report.warnings.append("Hyperliquid MAINNET: real money.")
    if problem := agent_problem(broker, network):
        report.ok = False
        report.errors.append(problem)
    try:
        closed = broker.close_unprotected_positions()
        if closed:
            report.warnings.append(
                f"closed position(s) found without a stop: {', '.join(closed)}")
        if broker.unclosed:
            report.ok = False
            report.errors.append(
                "position(s) without a stop are STILL OPEN; close by hand: " + "; ".join(
                    f"{c} ({why})" for c, why in broker.unclosed.items()))
        state = broker.info.user_state(broker.address)
        summary = state.get("marginSummary", {})
        report.usdt_balance = float(summary.get("accountValue", 0) or 0)
        report.usdt_available = float(state.get("withdrawable", 0) or 0)
        spot = unified_usdc(broker.info, broker.address)
        if spot is not None:
            report.usdt_balance += spot[0]
            report.usdt_available += spot[1]
        mids = broker.info.all_mids()
        report.last_price = float(mids[broker.coin]) if broker.coin in mids else None
        report.open_positions = [
            ap["position"] for ap in state.get("assetPositions", [])
            if float(ap.get("position", {}).get("szi", 0) or 0) != 0]
        report.open_orders = list(broker.info.frontend_open_orders(broker.address))
    except Exception as exc:  # noqa: BLE001
        report.ok = False
        report.errors.append(f"Hyperliquid read failed: {exc}")
        return report

    if report.last_price is None:
        report.ok = False
        report.errors.append(f"no mid price for {broker.coin}")
    if not report.usdt_balance:
        report.warnings.append("account value is 0; fund the testnet account before trading")
    return report


def smoke_test(broker: HyperliquidSDKBroker) -> tuple[bool, str]:
    """Place a limit that cannot fill, cancel it, confirm nothing is left."""
    mid = float(broker.info.all_mids()[broker.coin])
    px = round_price(mid * 0.5, broker.sz_decimals)
    step = 10 ** -broker.sz_decimals
    size = round((HYPERLIQUID_MIN_NOTIONAL_USD * MIN_SIZE_HEADROOM / px) // step * step + step,
                 broker.sz_decimals)
    before = {o["oid"] for o in broker.info.frontend_open_orders(broker.address)}
    resp = broker.exchange.order(broker.coin, True, size, px, {"limit": {"tif": "Gtc"}})
    statuses = order_statuses(resp)
    if not statuses or "resting" not in statuses[0]:
        return False, f"order was not accepted as resting: {statuses or resp}"
    oid = int(statuses[0]["resting"]["oid"])
    broker.exchange.cancel(broker.coin, oid)
    after = {o["oid"] for o in broker.info.frontend_open_orders(broker.address)}
    if oid in after or after - before:
        return False, f"order {oid} placed but still open after cancel: {sorted(after - before)}"
    if abs(broker.position_size()) > 0:
        return False, "a position exists after the smoke test; check the account"
    return True, f"placed buy {size} {broker.coin} @ {px} (oid {oid}), cancelled, nothing left open"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--smoke", action="store_true",
                   help="place a far-from-market limit order and cancel it")
    a = p.parse_args(argv)
    settings = get_settings()
    try:
        broker = HyperliquidSDKBroker(settings)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Could not connect: {}", exc)
        return 2
    report = run_diagnostics(settings, broker)
    for w in report.warnings:
        logger.warning(w)
    for e in report.errors:
        logger.critical(e)
    logger.info("network={} account value=${} mid={} positions={} orders={}",
                "MAINNET" if base_url(settings) == MAINNET_URL else "testnet",
                report.usdt_balance, report.last_price,
                len(report.open_positions), len(report.open_orders))
    if not report.ok:
        return 2
    if a.smoke:
        ok, message = smoke_test(broker)
        (logger.info if ok else logger.critical)("smoke test: {}", message)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
