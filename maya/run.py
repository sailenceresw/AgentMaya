"""Runner / CLI entrypoint for the Maya trading agent.

Wires together the data feed (Layer 1), the LangGraph pipeline, and the journal,
then streams candles through the graph one at a time. Defaults to ``sim`` mode
so ``python -m maya`` runs end-to-end with no API keys and no real orders.
"""
from __future__ import annotations

import argparse
import sys
import time

from loguru import logger

from maya.config import RiskLimits, RunMode, Settings, get_settings
from maya.data.ingestion import build_feed
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes


def _configure_logging(settings: Settings) -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")


def _guard_live(settings: Settings) -> None:
    """Refuse live trading unless the explicit interlock + credentials are set."""
    from maya.config import Venue

    if settings.mode is RunMode.LIVE and settings.live_confirm != "I_UNDERSTAND_THE_RISK":
        logger.critical(
            "LIVE mode requested but MAYA_LIVE_CONFIRM is not set to "
            "'I_UNDERSTAND_THE_RISK'. Refusing to trade real money."
        )
        sys.exit(2)
    if not settings.uses_real_orders:
        return
    if settings.venue is Venue.HYPERLIQUID:
        if not settings.hyperliquid_agent_key:
            logger.critical("Mode {} on Hyperliquid needs HYPERLIQUID_AGENT_KEY "
                            "(and HYPERLIQUID_ACCOUNT_ADDRESS). Aborting.", settings.mode.value)
            sys.exit(2)
    elif not (settings.binance_api_key and settings.binance_api_secret):
        logger.critical("Mode {} needs BINANCE_API_KEY/SECRET. Aborting.", settings.mode.value)
        sys.exit(2)


def check_connection(settings: Settings) -> int:
    """Verify connectivity to the configured venue and print a report."""
    from maya.config import Venue
    if settings.venue is Venue.HYPERLIQUID:
        return _check_hyperliquid(settings)
    return _check_binance(settings)


def _check_hyperliquid(settings: Settings) -> int:
    from maya.data.hyperliquid_client import connectivity_check

    net = "MAINNET" if settings.mode is RunMode.LIVE else "TESTNET"
    logger.info("Checking Hyperliquid connectivity ({}, coin={}) ...", net, settings.coin)
    res = connectivity_check(settings)
    if not res.ok:
        logger.error("Hyperliquid connection FAILED: {}", res.detail)
        logger.error("Could not reach Hyperliquid. Common causes:")
        logger.error("  1. Restricted egress — allow api.hyperliquid.xyz / "
                     "api.hyperliquid-testnet.xyz, or run on a host with access.")
        logger.error("  2. SDK not installed — pip install hyperliquid-python-sdk")
        return 1
    logger.info("Hyperliquid reachable — {}", res.detail)
    if res.last_price is not None:
        logger.info("Last {} mid price: {}", settings.coin, res.last_price)
    if res.authenticated:
        logger.info("Account {}… equity: {} USDC", settings.hyperliquid_account_address[:10],
                    res.equity_usdt)
    else:
        logger.info("No account address set — public data only. Set "
                    "HYPERLIQUID_ACCOUNT_ADDRESS to check your balance.")
    return 0


def _check_binance(settings: Settings) -> int:
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
    from maya.monitor.telegram import TelegramBot
    notifier = TelegramBot(settings)
    nodes = TradingNodes(settings, journal, notifier=notifier)
    graph = build_graph(nodes)
    feed = build_feed(settings)
    if notifier.enabled:
        notifier.send(f"🚀 Maya started — {settings.mode.value} {settings.symbol} "
                      f"{settings.timeframe}, equity ${nodes.risk.account.equity:.2f}")

    # State reconciliation on restart (spec §8.4): refuse to trade into an
    # unknown exchange state. If a position/orders already exist, HALT and
    # require manual verification rather than auto-resolving.
    if not _reconcile(settings, nodes, notifier):
        _report(nodes, journal, 0)
        journal.close()
        return

    tf_seconds = {"1m": 60, "5m": 300, "15m": 900}.get(settings.timeframe, 60)
    processed = 0
    last_candle = None
    prev_wall = time.monotonic()
    try:
        for candle in feed.candles():
            now_wall = time.monotonic()
            # Data-feed staleness circuit breaker (spec §7.3). On real feeds, if
            # a new candle takes far longer than its interval to arrive, halt
            # rather than trade on a stalled feed. (Sim/backtest use historical
            # timestamps, so this only applies to real-data modes.)
            if settings.uses_real_data and \
                    now_wall - prev_wall > tf_seconds + RiskLimits.DATA_STALE_SECONDS:
                logger.critical("Data feed stalled ({:.0f}s gap).", now_wall - prev_wall)
                nodes.risk.check_kill_switch(candle.timestamp, data_stale=True)
            prev_wall = now_wall

            graph.invoke({"candle": candle})
            last_candle = candle
            processed += 1

            # Handle operator commands from Telegram between candles (live only).
            if settings.uses_real_data and notifier.enabled:
                _handle_commands(notifier.poll_commands(), nodes, notifier, candle)

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


def _reconcile(settings: Settings, nodes: TradingNodes, notifier) -> bool:
    """Spec §8.4 — verify local vs exchange state at startup. Returns True if
    it is safe to start trading, False if an existing position/orders force a
    manual-verification halt."""
    if not settings.uses_real_orders:
        return True
    state = nodes.execution.broker.open_state()
    if state.get("position_qty", 0) > 0 or state.get("open_orders", 0) > 0:
        msg = (f"Reconciliation FAILED: exchange already has "
               f"{state.get('position_qty', 0)} position / "
               f"{state.get('open_orders', 0)} open orders. HALTING for manual "
               f"verification (spec §8.4) — not starting.")
        logger.critical(msg)
        nodes.risk.manual_halt()
        if notifier.enabled:
            notifier.send("🛑 " + msg)
        return False
    logger.info("Reconciliation OK — exchange is flat.")
    return True


def _handle_commands(cmds, nodes, notifier, candle) -> None:
    """Act on Telegram slash-commands (spec §10.1.2)."""
    acc = nodes.risk.account
    for cmd in cmds:
        if cmd == "halt":
            nodes.risk.manual_halt()
            notifier.send("🛑 Trading halted by command.")
        elif cmd == "resume":
            nodes.risk.resume()
            notifier.send("▶️ Trading resumed.")
        elif cmd == "close_all":
            if nodes.execution.has_position:
                r = nodes.execution.force_close(candle.close, candle.timestamp)
                if r:
                    nodes.journal.record(r, r.position.confluences)
                    nodes.risk.on_trade_closed(r.pnl_usdt, candle.timestamp)
                    notifier.send(f"Closed position: PnL {r.pnl_usdt:+.2f}")
            else:
                notifier.send("No open position.")
        elif cmd in ("status", "risk"):
            pos = "none" if not nodes.execution.has_position else \
                f"{nodes.execution.position.direction} {nodes.execution.position.symbol}"
            notifier.send(
                f"state: {acc.state.value}\nequity: ${acc.equity:.2f}\n"
                f"open: {pos}\ntrades today: {acc.trades_today}\n"
                f"consec losses: {acc.consecutive_losses}")
        elif cmd == "stats":
            m = nodes.journal.metrics()
            if m.get("trades"):
                notifier.summary(
                    f"{m['trades']} trades · win {m['win_rate']}% · "
                    f"PF {m['profit_factor']} · PnL ${m['total_pnl_usdt']}")
            else:
                notifier.summary("No trades yet.")
        else:
            notifier.send(f"Unknown command /{cmd}. Try /status /halt /resume "
                          f"/close_all /stats /risk")


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
