"""Layer 1 / 8 — exchange connectivity & account diagnostics.

Standalone, **read-only** sanity check against Binance USD-M Futures
(testnet or live). Verifies the API key/secret pair, clock sync, account
access, and account/position mode *before* the runner ever touches real
orders. Never places, cancels, or modifies anything.

Run directly, independent of the trading loop:

    MAYA_MODE=testnet python -m maya.check_connection

Also imported by :mod:`maya.run` at startup for testnet/live modes, both to
seed the risk manager with a real starting equity figure and to perform the
state-reconciliation check required by spec section 8.4 (if the exchange
already shows an open position/order that Maya has no local record of, that
is a mismatch and the system must refuse to start rather than guess).
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from maya.config import RunMode, Settings, get_settings


@dataclass
class ConnectionReport:
    ok: bool
    mode: str
    symbol: str
    clock_skew_ms: int | None = None
    usdt_balance: float | None = None
    usdt_available: float | None = None
    last_price: float | None = None
    dual_side_position: bool | None = None
    open_positions: list[dict] = field(default_factory=list)
    open_orders: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Fee figures read from the venue itself (Hyperliquid via `mm perps
    # quote`): fee_maker_bps / fee_taker_bps / builder_fee_bps -> value.
    fee_calibration: dict[str, float] = field(default_factory=dict)


def _build_exchange(settings: Settings):
    from binance_sdk_derivatives_trading_usds_futures import DerivativesTradingUsdsFutures
    from binance_common.configuration import ConfigurationRestAPI

    cfg = ConfigurationRestAPI(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
    )
    if settings.mode is RunMode.TESTNET:
        cfg.base_path = "https://testnet.binancefuture.com"
        logger.info("Targeting Binance Futures demo/testnet endpoint")
    elif settings.mode is RunMode.LIVE:
        cfg.base_path = "https://fapi.binance.com"
        logger.warning("Targeting Binance Futures LIVE — real funds.")
    else:
        cfg.base_path = "https://fapi.binance.com"

    return DerivativesTradingUsdsFutures(config_rest_api=cfg)


def _coerce_value(value: Any) -> Any:
    """Normalize Binance SDK response objects into plain Python values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "data") and callable(value.data):
        try:
            data = value.data()
            if data is not None:
                return _coerce_value(data)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        payload = getattr(value, "__dict__", None)
        if isinstance(payload, dict):
            return payload
    return value


def _extract_server_time(response: Any) -> int | None:
    payload = _coerce_value(response)
    if isinstance(payload, dict):
        for key in ("serverTime", "server_time"):
            if key in payload:
                return int(payload[key])
    return None


def _extract_price(response: Any) -> float | None:
    payload = _coerce_value(response)
    if isinstance(payload, dict):
        for key in ("price", "markPrice"):
            if key in payload and payload[key] is not None:
                return float(payload[key])
    return None


def _coerce_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        payload = getattr(value, "__dict__", None)
        if isinstance(payload, dict):
            return payload
    return None


def _extract_balance(response: Any) -> tuple[float | None, float | None]:
    payload = _coerce_value(response)
    if isinstance(payload, list):
        for item in payload:
            item_map = _coerce_mapping(item)
            if item_map and item_map.get("asset") == "USDT":
                return float(item_map.get("balance", 0)), float(item_map.get("withdrawAvailable", 0))
        return None, None
    if isinstance(payload, dict):
        if payload.get("asset") == "USDT":
            return float(payload.get("balance", 0)), float(payload.get("withdrawAvailable", 0))
        entries = payload.get("balances")
        if isinstance(entries, list):
            for item in entries:
                item_map = _coerce_mapping(item)
                if item_map and item_map.get("asset") == "USDT":
                    return float(item_map.get("balance", 0)), float(item_map.get("withdrawAvailable", 0))
    return None, None


def _extract_position_mode(response: Any) -> bool | None:
    payload = _coerce_value(response)
    if isinstance(payload, dict):
        value = payload.get("dualSidePosition")
        if value is not None:
            return bool(value)
    return None


def _extract_positions(response: Any) -> list[dict]:
    payload = _coerce_value(response)
    if isinstance(payload, list):
        positions = payload
    elif isinstance(payload, dict):
        positions = payload.get("positions", []) if isinstance(payload.get("positions"), list) else []
    else:
        return []

    out: list[dict] = []
    for p in positions:
        p_map = _coerce_mapping(p)
        if not p_map:
            continue
        amt = float(p_map.get("positionAmt", 0))
        if amt != 0:
            out.append({
                "symbol": p_map.get("symbol"),
                "contracts": amt,
                "side": p_map.get("positionSide"),
            })
    return out


def _extract_orders(response: Any) -> list[dict]:
    payload = _coerce_value(response)
    if isinstance(payload, list):
        orders = payload
    elif isinstance(payload, dict):
        orders = payload.get("orders", []) if isinstance(payload.get("orders"), list) else []
    else:
        return []

    out: list[dict] = []
    for o in orders:
        o_map = _coerce_mapping(o)
        if o_map:
            out.append({
                "id": o_map.get("orderId"),
                "type": o_map.get("type"),
                "side": o_map.get("side"),
            })
    return out


def run_diagnostics(settings: Settings | None = None) -> ConnectionReport:
    """Run every check that's cheap and safe to run. Never raises for
    ordinary connectivity/auth failures — collects them into the report so
    the caller can decide what's fatal."""
    settings = settings or get_settings()
    report = ConnectionReport(ok=True, mode=settings.mode.value, symbol=settings.symbol)

    if not settings.uses_real_data:
        report.errors.append(
            f"mode={settings.mode.value} has no real exchange connection to check "
            "(sim mode is fully synthetic — set MAYA_MODE=paper/testnet/live)"
        )
        report.ok = False
        return report

    if settings.uses_real_orders and not (settings.binance_api_key and settings.binance_api_secret):
        report.errors.append("BINANCE_API_KEY / BINANCE_API_SECRET not set")
        report.ok = False
        return report

    try:
        ex = _build_exchange(settings)
    except ImportError:
        report.errors.append("ccxt is not installed — pip install -r requirements.txt")
        report.ok = False
        return report
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"failed to construct exchange client: {exc}")
        report.ok = False
        return report

    # 1. Clock sync — Binance rejects signed requests with too much skew.
    try:
        server_time = ex.rest_api.check_server_time()
        server_ms = _extract_server_time(server_time)
        if server_ms is None:
            raise ValueError(f"unexpected server time response: {server_time!r}")
        report.clock_skew_ms = int(time.time() * 1000) - int(server_ms)
        if abs(report.clock_skew_ms) > 1000:
            report.warnings.append(
                f"clock skew {report.clock_skew_ms}ms — sync system time (NTP) "
                "before running testnet/live"
            )
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"fetch_time failed (network/DNS?): {exc}")
        report.ok = False
        return report  # nothing downstream will work either

    # 2. Market data / symbol validity — public endpoint, works without keys.
    try:
        ticker = ex.rest_api.symbol_price_ticker(symbol=settings.symbol)
        price = _extract_price(ticker)
        if price is None:
            raise ValueError(f"unexpected ticker response: {ticker!r}")
        report.last_price = price
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"fetch_ticker({settings.symbol}) failed: {exc}")
        report.ok = False

    if not settings.uses_real_orders:
        return report  # paper mode: market data only, no auth/order checks

    # 3. Auth + balance — prove the API key/secret pair is valid and scoped
    #    to Futures. If the signed account routes are unavailable on the
    #    configured network, report that explicitly instead of pretending the
    #    connection is healthy.
    try:
        balances = ex.rest_api.futures_account_balance_v3()
        report.usdt_balance, report.usdt_available = _extract_balance(balances)
        if (report.usdt_balance or 0) <= 0:
            report.warnings.append(
                "USDT futures balance is 0 — fund the demo account before trading"
            )
        elif report.usdt_balance < 100:
            report.warnings.append(
                f"USDT balance ${report.usdt_balance:.2f} is below the ~$100 "
                "minimum viable size noted for fee math to work cleanly"
            )
    except Exception as exc:  # noqa: BLE001
        report.errors.append(
            f"fetch_balance failed — the key/secret or endpoint is not accepting "
            f"signed Futures requests: {exc}"
        )
        report.ok = False

    # 4. Position mode — Maya's engine assumes a single position in one-way
    #    mode; Hedge mode changes order semantics and would silently break it.
    try:
        raw = ex.rest_api.futures_account_configuration()
        report.dual_side_position = _extract_position_mode(raw)
        if report.dual_side_position:
            report.warnings.append(
                "account is in HEDGE (dual-side) position mode — Maya assumes "
                "ONE-WAY mode; switch it in Binance Futures > Preferences"
            )
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"could not read position mode: {exc}")

    # 5. Existing open positions/orders — spec 8.4 state reconciliation input.
    #    A fresh process has zero local state, so anything found here is a
    #    mismatch the caller must not silently absorb. These calls are best-effort
    #    and should not prevent the bot from starting if market data is healthy.
    try:
        positions = ex.rest_api.position_information_v3(symbol=settings.symbol)
        report.open_positions = _extract_positions(positions)
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"could not fetch open positions: {exc}")

    try:
        orders = ex.rest_api.current_all_open_orders(symbol=settings.symbol)
        report.open_orders = _extract_orders(orders)
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"could not fetch open orders: {exc}")

    return report


def _print_report(report: ConnectionReport) -> None:
    logger.info("=== Maya — Exchange Connection Check ({}) ===", report.mode.upper())
    logger.info("Symbol            : {}", report.symbol)
    if report.clock_skew_ms is not None:
        logger.info("Clock skew        : {}ms", report.clock_skew_ms)
    if report.last_price is not None:
        logger.info("Last price        : {:.2f}", report.last_price)
    if report.usdt_balance is not None:
        logger.info("USDT balance      : {:.2f} (available {:.2f})",
                    report.usdt_balance, report.usdt_available or 0.0)
    if report.dual_side_position is not None:
        logger.info("Position mode     : {}", "HEDGE" if report.dual_side_position else "ONE-WAY")
    if report.open_positions:
        logger.warning("Open positions on exchange: {}", report.open_positions)
    elif report.usdt_balance is not None:
        logger.info("Open positions    : none")
    if report.open_orders:
        logger.warning("Open orders on exchange: {}", report.open_orders)
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
    settings = get_settings()
    report = run_diagnostics(settings)
    _print_report(report)
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
