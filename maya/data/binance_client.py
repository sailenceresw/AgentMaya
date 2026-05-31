"""Binance USD-M Futures client helpers (ccxt-backed).

Centralises everything Binance-specific so the feed and broker share one correct
implementation:

* unified-symbol conversion — ccxt's ``binanceusdm`` expects ``BTC/USDT:USDT``,
  not ``BTCUSDT``;
* exchange construction (prod or testnet sandbox);
* a :func:`connectivity_check` used by ``python -m maya --check``.

ccxt is imported lazily so the default ``sim`` mode never requires it.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from maya.config import RunMode, Settings

# Quote assets Binance USD-M perpetuals settle in, longest-first for matching.
_QUOTES = ("USDT", "USDC", "BUSD", "USD")


def to_ccxt_symbol(symbol: str) -> str:
    """Convert an exchange symbol like ``BTCUSDT`` to ccxt's ``BTC/USDT:USDT``.

    Already-unified symbols (containing ``/``) are returned unchanged.
    """
    if "/" in symbol:
        return symbol
    s = symbol.upper().replace("-", "").replace("_", "")
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            base = s[: -len(q)]
            return f"{base}/{q}:{q}"
    raise ValueError(f"Cannot parse Binance symbol '{symbol}' into a ccxt symbol")


def build_exchange(settings: Settings):
    """Create a configured ccxt ``binanceusdm`` instance (prod or testnet)."""
    import ccxt

    ex = ccxt.binanceusdm({
        "apiKey": settings.binance_api_key,
        "secret": settings.binance_api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "future", "adjustForTimeDifference": True},
    })
    if settings.mode is RunMode.TESTNET:
        ex.set_sandbox_mode(True)
        logger.warning("Binance client in TESTNET sandbox mode")
    return ex


@dataclass
class ConnectivityResult:
    ok: bool
    detail: str
    authenticated: bool = False
    last_price: float | None = None
    equity_usdt: float | None = None


def connectivity_check(settings: Settings) -> ConnectivityResult:
    """Verify we can reach Binance and (if keyed) read the account.

    Public market data is checked first (no keys needed). If API credentials are
    present, the private account endpoint is also checked. Network/permission
    failures are reported rather than raised.
    """
    try:
        import ccxt
    except ImportError:
        return ConnectivityResult(False, "ccxt not installed (pip install ccxt)")

    sym = to_ccxt_symbol(settings.symbol)
    try:
        ex = build_exchange(settings)
        ex.load_markets()
        ticker = ex.fetch_ticker(sym)
        last = float(ticker["last"]) if ticker.get("last") else None
    except Exception as exc:  # noqa: BLE001
        return ConnectivityResult(False, f"public data fetch failed: {exc}")

    result = ConnectivityResult(True, "public market data OK", last_price=last)

    if settings.binance_api_key and settings.binance_api_secret:
        try:
            bal = ex.fetch_balance()
            equity = float(bal.get("USDT", {}).get("total") or 0.0)
            result.authenticated = True
            result.equity_usdt = equity
            result.detail = "public + private (account) OK"
        except Exception as exc:  # noqa: BLE001
            result.detail = f"public OK, but account read failed: {exc}"
    return result
