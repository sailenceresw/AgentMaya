"""Runner / CLI entrypoint for the Maya trading agent.

Wires together the data feed (Layer 1), the LangGraph pipeline, and the journal,
then streams candles through the graph one at a time. Defaults to ``sim`` mode
so ``python -m maya`` runs end-to-end with no API keys and no real orders.
"""
from __future__ import annotations

import argparse
import sys

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
    if settings.uses_real_orders and not (settings.binance_api_key and settings.binance_api_secret):
        logger.critical("Mode {} needs BINANCE_API_KEY/SECRET. Aborting.", settings.mode.value)
        sys.exit(2)


def check_connection(settings: Settings) -> int:
    """Verify connectivity to the Binance API and print a report. Returns exit code."""
    from maya.data.binance_client import connectivity_check, to_ccxt_symbol

    logger.info("Checking Binance connectivity (mode={}, symbol={} -> {}) ...",
                settings.mode.value, settings.symbol, to_ccxt_symbol(settings.symbol))
    res = connectivity_check(settings)
    if not res.ok:
        logger.error("Binance connection FAILED: {}", res.detail)
        logger.error("Could not reach the Binance API. Common causes:")
        logger.error("  1. Restricted egress (proxy/firewall). Allow fapi.binance.com "
                     "and testnet.binancefuture.com, or run on a host with Binance access.")
        logger.error("  2. Region block — Binance Futures is unavailable in some "
                     "jurisdictions (e.g. the US). Use a permitted region/VPS.")
        logger.error("  3. Wrong mode/keys — testnet needs testnet keys; live needs "
                     "futures-enabled keys.")
        return 1
    logger.info("Binance reachable — {}", res.detail)
    if res.last_price is not None:
        logger.info("Last {} price: {}", settings.symbol, res.last_price)
    if res.authenticated:
        logger.info("Account authenticated. USDT equity: {}", res.equity_usdt)
    else:
        logger.info("No API keys set — public data only (fine for sim/paper data).")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maya", description="Maya order-flow trading agent")
    parser.add_argument("--check", action="store_true",
                        help="Verify Binance API connectivity and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    _configure_logging(settings)

    if args.check:
        sys.exit(check_connection(settings))

    _guard_live(settings)

    logger.info("=== Maya Trading System === mode={} symbol={} tf={}",
                settings.mode.value, settings.symbol, settings.timeframe)
    if settings.mode is not RunMode.SIM:
        logger.warning("Running in {} mode.", settings.mode.value)

    journal = TradeJournal()
    nodes = TradingNodes(settings, journal)
    graph = build_graph(nodes)
    feed = build_feed(settings)

    processed = 0
    last_candle = None
    try:
        for candle in feed.candles():
            graph.invoke({"candle": candle})
            last_candle = candle
            processed += 1
            if nodes.risk.account.state.value == "halted":
                logger.critical("System HALTED ({}). Stopping new candles.",
                                ", ".join(nodes.risk.account.halt_reasons))
                break
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        # Flatten any open position at the last market price (clean shutdown).
        if nodes.execution.has_position and last_candle is not None:
            result = nodes.execution.force_close(last_candle.close, last_candle.timestamp)
            if result:
                journal.record(result, result.position.confluences)
                nodes.risk.on_trade_closed(result.pnl_usdt, last_candle.timestamp)

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
