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
    if settings.uses_metamask and settings.perps_dry_run:
        logger.warning("MAYA_PERPS_DRY_RUN: orders are validated and priced on Hyperliquid "
                       "{} but NEVER signed or submitted; positions are tracked locally.",
                       "mainnet" if settings.is_live else "testnet")
    if settings.uses_metamask:
        # The Hyperliquid path is self-custodial — there are no exchange API
        # keys to check here. The equivalent gate (an authenticated `mm`
        # session) is enforced by maya.check_metamask during reconciliation.
        return
    if settings.uses_real_orders and not (settings.binance_api_key and settings.binance_api_secret):
        logger.critical("Mode {} needs BINANCE_API_KEY/SECRET. Aborting.", settings.mode.value)
        sys.exit(2)


def _apply_fee_calibration(settings: Settings, quoted: dict[str, float]) -> None:
    """Use the fees the venue quoted unless the user pinned their own.

    Applied before any risk manager or engine exists, so the fee gate and
    every PnL figure use the real numbers from the first candle."""
    for key, value in quoted.items():
        if getattr(settings, key) is None and settings.fee_rate is None:
            setattr(settings, key, value)
            logger.info("Fee model: {} = {:.2f}bps (from `mm perps quote`)", key, value)


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
    _apply_fee_calibration(settings, report.fee_calibration)
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


def _start_watchdog(settings: Settings, risk) -> "object | None":
    """Halt new trades if a real feed goes silent (spec 7.3). Sim and backtest
    replay history at full speed, so they have no wall clock to watch."""
    if not settings.uses_real_data:
        return None
    from maya.data.watchdog import FeedWatchdog

    return FeedWatchdog(
        settings.timeframe,
        on_stale=lambda gap: risk.check_kill_switch(int(time.time() * 1000),
                                                    data_stale=True),
    ).start()


def _start_api(settings: Settings, nodes: TradingNodes, journal: TradeJournal,
               processed_fn) -> "object | None":
    """Start the Layer 7 monitoring API, if enabled.

    Never fatal: a dashboard failing to start is not a reason to stop an agent
    that is managing real positions, so any problem is logged and trading
    continues without it.
    """
    if not settings.api_enabled:
        return None
    try:
        from maya.api.server import MonitorServer, build_snapshot

        server = MonitorServer(
            settings,
            snapshot_fn=lambda: build_snapshot(settings, nodes, journal, processed_fn()),
            trades_fn=journal.recent,
            halt_fn=lambda reason: nodes.risk.manual_halt(),
        )
        server.start()
        return server
    except Exception as exc:  # noqa: BLE001 - the dashboard must never stop trading
        logger.error("monitoring API failed to start — trading continues: {}", exc)
        return None


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
    api = _start_api(settings, nodes, journal, lambda: processed)
    watchdog = _start_watchdog(settings, nodes.risk)
    last_candle = None
    try:
        for candle in feed.candles():
            last_candle = candle
            if watchdog is not None:
                watchdog.beat()
            graph.invoke({"candle": candle})
            processed += 1
            if nodes.risk.account.state.value == "halted":
                logger.critical("System HALTED ({}). Stopping new candles.",
                                ", ".join(nodes.risk.account.halt_reasons))
                break
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        if watchdog is not None:
            watchdog.stop()
        if monitor is not None:
            monitor.stop()
        if api is not None:
            api.stop()

        # Flatten any open position at the last seen price (clean shutdown).
        if nodes.execution.has_position:
            from maya.execution.engine import flatten_on_shutdown

            pos = nodes.execution.position
            price = last_candle.close if last_candle is not None else pos.entry_price
            flatten_on_shutdown(nodes.execution, nodes.risk, journal, price,
                                int(time.time() * 1000))

        _report(nodes, journal, processed)
        journal.close()


def _report(nodes: TradingNodes, journal: TradeJournal, processed: int) -> None:
    """Summarize the run.

    Reports **this run** separately from the all-time journal. They are not the
    same number: the journal is durable across restarts by design (it backs the
    spec-8.4 risk-state restoration), so its aggregates mix every previous run
    — including sim runs mixed in with testnet and live ones. Presenting those
    as the run summary is actively misleading, and would corrupt the §14.1
    profit-factor / win-rate gates.
    """
    acc = nodes.risk.account
    run = journal.metrics(session_only=True)
    lifetime = journal.metrics()

    logger.info("------------------------------------------------------------")
    logger.info("Candles processed : {}", processed)
    logger.info("Final equity      : ${:.2f} (start ${:.2f})",
                acc.equity, acc.start_equity)
    logger.info("System state      : {}", acc.state.value)

    if run.get("trades", 0):
        logger.info("--- this run ---")
        logger.info("Trades            : {}", run["trades"])
        logger.info("Win rate          : {}%", run["win_rate"])
        logger.info("Profit factor     : {}", _pf(run["profit_factor"]))
        logger.info("Total PnL         : ${}", run["total_pnl_usdt"])
        logger.info("Expectancy/trade  : ${}", run["expectancy"])
        logger.info("Total fees        : ${}", run["total_fees"])
    else:
        logger.info("No trades were taken in this run.")

    # Only worth printing when it says something the run summary doesn't.
    older = lifetime.get("trades", 0) - run.get("trades", 0)
    if older > 0:
        logger.info("--- journal all-time ({} earlier trade(s) from previous runs) ---",
                    older)
        logger.info("Trades            : {}", lifetime["trades"])
        logger.info("Win rate          : {}%", lifetime["win_rate"])
        logger.info("Profit factor     : {}", _pf(lifetime["profit_factor"]))
        logger.info("Total PnL         : ${}", lifetime["total_pnl_usdt"])
    logger.info("------------------------------------------------------------")


def _pf(value) -> str:
    return "∞ (no losses)" if value is None else str(value)


def _report_scan(risk, journal: TradeJournal) -> None:
    """Run summary for the scanner agent, fees broken out: the number that
    says whether the trades paid for themselves."""
    run = journal.metrics(session_only=True)
    acc = risk.account
    logger.info("------------------------------------------------------------")
    logger.info("Final equity      : ${:.2f} (start ${:.2f})", acc.equity, acc.start_equity)
    if run.get("trades", 0):
        gross = run["total_pnl_usdt"] + run["total_fees"]
        logger.info("Trades            : {} (win rate {}%)", run["trades"], run["win_rate"])
        logger.info("PnL before fees   : ${:.2f}", gross)
        logger.info("Fees paid         : ${}", run["total_fees"])
        logger.info("PnL after fees    : ${}", run["total_pnl_usdt"])
        if gross > 0:
            logger.info("Fees took         : {:.0%} of gross profit", run["total_fees"] / gross)
    else:
        logger.info("No trades were taken in this run.")
    logger.info("------------------------------------------------------------")


if __name__ == "__main__":
    main()
