"""Runner / CLI entrypoint for the Maya trading agent.

Wires together the data feed (Layer 1), the LangGraph pipeline, and the journal,
then streams candles through the graph one at a time. Defaults to ``sim`` mode
so ``python -m maya`` runs end-to-end with no API keys and no real orders.
"""
from __future__ import annotations

import sys
import time

from loguru import logger

from maya.config import RunMode, Settings, get_settings
from maya.data.ingestion import build_feed
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes


def _configure_logging(settings: Settings) -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")


def _guard_live(settings: Settings) -> None:
    """Refuse live trading unless the explicit interlock is set (spec section 7)."""
    if settings.mode is RunMode.LIVE and settings.live_confirm != "I_UNDERSTAND_THE_RISK":
        logger.critical(
            "LIVE mode requested but MAYA_LIVE_CONFIRM is not set to "
            "'I_UNDERSTAND_THE_RISK'. Refusing to trade real money."
        )
        sys.exit(2)
    if settings.uses_metamask:
        # The Hyperliquid path is self-custodial — there are no exchange API
        # keys to check here. The equivalent gate (an authenticated `mm`
        # session) is enforced by maya.check_metamask during reconciliation.
        return
    if settings.uses_real_orders and not (settings.binance_api_key and settings.binance_api_secret):
        logger.critical("Mode {} needs BINANCE_API_KEY/SECRET. Aborting.", settings.mode.value)
        sys.exit(2)


def _startup_reconciliation(settings: Settings) -> float:
    """Connectivity + state reconciliation gate (spec section 8.4) for any
    mode that talks to a real exchange. Returns the equity to seed the risk
    manager with. Refuses to start (sys.exit) rather than guess if the
    exchange disagrees with Maya's (empty, on a fresh process) local state.
    """
    if not settings.uses_real_data:
        return settings.start_equity  # sim mode: nothing to check

    if settings.uses_metamask:
        from maya.check_metamask import run_diagnostics
        logger.info("Running MetaMask / Hyperliquid connection check before starting...")
    else:
        from maya.check_connection import run_diagnostics
        logger.info("Running exchange connection check before starting...")

    report = run_diagnostics(settings)
    for w in report.warnings:
        logger.warning(w)
    if not report.ok:
        for e in report.errors:
            logger.critical(e)
        checker = "maya.check_metamask" if settings.uses_metamask else "maya.check_connection"
        logger.critical("Connection check failed — refusing to start {} mode. "
                        "Run `python -m {}` for details.",
                        settings.mode.value, checker)
        sys.exit(2)

    if settings.uses_real_orders:
        # Fresh process = zero locally-tracked position/orders. If the
        # exchange disagrees, spec 8.4 is explicit: HALT and require a
        # human — never auto-resolve by adopting or closing a mystery
        # position on startup.
        if report.open_positions or report.open_orders:
            logger.critical(
                "STATE MISMATCH on startup: exchange shows {} open position(s) "
                "and {} open order(s) for {}, but Maya has no local record. "
                "Refusing to start — resolve manually on the exchange (or in "
                "the journal, if this is expected) and restart. Details: "
                "positions={} orders={}",
                len(report.open_positions), len(report.open_orders),
                settings.symbol, report.open_positions, report.open_orders,
            )
            sys.exit(2)
        if report.usdt_balance is not None and report.usdt_balance > 0:
            logger.info("Seeding equity from exchange balance: ${:.2f} "
                        "(config default was ${:.2f})",
                        report.usdt_balance, settings.start_equity)
            return report.usdt_balance

    return settings.start_equity


def main() -> None:
    settings = get_settings()
    _configure_logging(settings)
    _guard_live(settings)

    logger.info("=== Maya Trading System === mode={} symbol={} tf={}",
                settings.mode.value, settings.symbol, settings.timeframe)
    if settings.mode is not RunMode.SIM:
        logger.warning("Running in {} mode.", settings.mode.value)

    equity = _startup_reconciliation(settings)
    now_ts = int(time.time() * 1000)

    journal = TradeJournal()
    nodes = TradingNodes(settings, journal, equity=equity, now_ts=now_ts)
    graph = build_graph(nodes)
    feed = build_feed(settings)

    monitor = None
    if settings.uses_real_orders:
        from maya.execution.monitor import PositionMonitor
        monitor = PositionMonitor(settings, nodes.execution, journal, nodes.risk)
        monitor.start()

    processed = 0
    try:
        for candle in feed.candles():
            graph.invoke({"candle": candle})
            processed += 1
            if nodes.risk.account.state.value == "halted":
                logger.critical("System HALTED ({}). Stopping new candles.",
                                ", ".join(nodes.risk.account.halt_reasons))
                break
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        if monitor is not None:
            monitor.stop()

        # Flatten any open position at the last seen price (clean shutdown).
        if nodes.execution.has_position:
            last_price = nodes.execution.position.entry_price
            result = nodes.execution.force_close(last_price, 0)
            if result:
                journal.record(result, result.position.confluences)

        _report(nodes, journal, processed)
        journal.close()


def _report(nodes: TradingNodes, journal: TradeJournal, processed: int) -> None:
    acc = nodes.risk.account
    metrics = journal.metrics()
    logger.info("------------------------------------------------------------")
    logger.info("Candles processed : {}", processed)
    logger.info("Final equity      : ${:.2f} (start ${:.2f})",
                acc.equity, acc.start_equity)
    logger.info("System state      : {}", acc.state.value)
    if metrics.get("trades", 0):
        logger.info("Trades            : {}", metrics["trades"])
        logger.info("Win rate          : {}%", metrics["win_rate"])
        logger.info("Profit factor     : {}", metrics["profit_factor"])
        logger.info("Total PnL         : ${}", metrics["total_pnl_usdt"])
        logger.info("Expectancy/trade  : ${}", metrics["expectancy"])
        logger.info("Total fees        : ${}", metrics["total_fees"])
    else:
        logger.info("No trades were taken in this run.")
    logger.info("------------------------------------------------------------")


if __name__ == "__main__":
    main()
