"""Hyperliquid client helpers (on-chain perps).

Centralises Hyperliquid-specific setup so the feed and broker share one correct
implementation:

* ``Info``  — public market data (candles, order book, account state), no key.
* ``Exchange`` — signs orders with an AGENT/API wallet (approved once from your
  main wallet) that can trade but cannot withdraw. Your main seed/withdrawal key
  is never used here.

The SDK (``hyperliquid-python-sdk``) and ``eth_account`` are imported lazily so
the default ``sim`` mode needs neither installed.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from maya.config import RunMode, Settings


def base_url(settings: Settings) -> str:
    from hyperliquid.utils import constants
    # Hyperliquid testnet is the sandbox; mainnet is real money.
    if settings.mode is RunMode.LIVE:
        return constants.MAINNET_API_URL
    return constants.TESTNET_API_URL


def build_info(settings: Settings):
    """Public market-data client (no wallet needed)."""
    from hyperliquid.info import Info
    return Info(base_url(settings), skip_ws=True)


def build_exchange(settings: Settings):
    """Order-signing client using the agent wallet."""
    from eth_account import Account
    from hyperliquid.exchange import Exchange

    if not settings.hyperliquid_agent_key:
        raise ValueError("HYPERLIQUID_AGENT_KEY is required to sign orders")
    wallet = Account.from_key(settings.hyperliquid_agent_key)
    account = settings.hyperliquid_account_address or wallet.address
    ex = Exchange(wallet, base_url(settings), account_address=account)
    logger.info("Hyperliquid exchange ready (account {}, agent {}, {})",
                account[:10] + "…", wallet.address[:10] + "…",
                "MAINNET" if settings.mode is RunMode.LIVE else "TESTNET")
    return ex


@dataclass
class ConnectivityResult:
    ok: bool
    detail: str
    authenticated: bool = False
    last_price: float | None = None
    equity_usdt: float | None = None


def connectivity_check(settings: Settings) -> ConnectivityResult:
    """Verify Hyperliquid reachability and (if an address is set) the account."""
    try:
        import hyperliquid  # noqa: F401
    except ImportError:
        return ConnectivityResult(False, "hyperliquid-python-sdk not installed")

    coin = settings.coin
    try:
        info = build_info(settings)
        mids = info.all_mids()
        last = float(mids.get(coin)) if mids.get(coin) is not None else None
    except Exception as exc:  # noqa: BLE001
        return ConnectivityResult(False, f"public data fetch failed: {exc}")

    result = ConnectivityResult(True, "public market data OK", last_price=last)

    addr = settings.hyperliquid_account_address
    if addr:
        try:
            state = info.user_state(addr)
            equity = float(state["marginSummary"]["accountValue"])
            result.authenticated = True
            result.equity_usdt = equity
            result.detail = "public + account state OK"
        except Exception as exc:  # noqa: BLE001
            result.detail = f"public OK, but account read failed: {exc}"
    return result
