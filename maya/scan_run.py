"""Scanner-driven agent: watch many coins, hold at most one trade.

Each time a candle of ``MAYA_TIMEFRAME`` closes:

* **flat** — :class:`~maya.scanner.Scanner` reads every coin in the universe,
  and the best candidate that clears the strategy, the chart desk, the
  fee-aware risk gate and the LLM veto (if enabled) is opened;
* **in a position** — that coin's newly closed candles are fed to the
  execution engine, which applies the stop/target, the breakeven move and
  the max-duration close exactly as the single-symbol agent does.

Everything below the choice of coin is the existing machinery: the same
risk manager and hard limits (one position, daily caps, kill switch), the
same brokers (paper, Binance, MetaMask/Hyperliquid), the same journal, and
the same venue position monitor for real orders.

    MAYA_MODE=paper MAYA_TIMEFRAME=15m python -m maya.scan_run
"""
from __future__ import annotations

import sys
import time

from loguru import logger

from maya.config import RunMode, Settings, get_settings
from maya.data.models import Candle
from maya.execution.engine import ExecutionEngine
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager, SystemState
from maya.scanner import Candidate, Scanner, format_table
from maya.timeframe import timeframe_ms


class ScanAgent:
    """One scan-or-manage step per closed candle. Clock-free, so testable."""

    def __init__(self, settings: Settings, journal: TradeJournal, scanner: Scanner,
                 execution: ExecutionEngine, risk: RiskManager) -> None:
        self.settings = settings
        self.journal = journal
        self.scanner = scanner
        self.execution = execution
        self.risk = risk
        self._last_managed_ts = 0
        self.last_scan: list[Candidate] = []   # read by the monitoring API
        self.steps = 0
        self.got_data = False                  # did the last step read any candles?

    def step(self, now_ts: int) -> Candidate | None:
        """Advance one candle. Returns the candidate opened, if any."""
        self.steps += 1
        self.got_data = False
        self.risk.check_kill_switch(now_ts)
        if self.execution.has_position:
            self._manage(now_ts)
            return None
        if self.risk.account.state is SystemState.HALTED:
            return None

        results = self.scanner.scan(now_ts)
        self.last_scan = results
        self.got_data = any(not c.rejected.startswith("no candles") for c in results)
        logger.info("scan:\n{}", format_table(results))
        best = self.scanner.review_best(results)
        if best is None:
            logger.info("no coin offers a trade that pays after fees this candle")
            return None
        if self.execution.open(best.intent, best.decision, now_ts):
            self.risk.on_trade_opened()
            self._last_managed_ts = now_ts
            logger.info("OPENED {} {} via {} — net R:R {:.2f} after fees, desk {}",
                        best.market.coin, best.intent.direction, best.intent.setup_name,
                        best.net_rr, best.desk_verdict)
            return best
        return None

    def _manage(self, now_ts: int) -> None:
        pos = self.execution.position
        try:
            candles = self.scanner.fetch_candles(pos.symbol, 5)
        except Exception as exc:  # noqa: BLE001 - the venue still holds the stop
            logger.warning("could not fetch candles for open {}: {}", pos.symbol, exc)
            return
        self.got_data = bool(candles)
        for candle in candles:
            if candle.timestamp <= max(self._last_managed_ts, pos.opened_ts):
                continue
            self._last_managed_ts = candle.timestamp
            result = self.execution.manage(candle)
            if result is not None:
                self._record(result, candle)
                break

    def _record(self, result, candle: Candle) -> None:
        self.journal.record(result, result.position.confluences)
        self.risk.on_trade_closed(result.pnl_usdt, candle.timestamp)
        logger.info("Trade closed: {} {} {} pnl={:.2f} fees={:.2f} ({})",
                    result.position.symbol, result.position.setup_name, result.result,
                    result.pnl_usdt, result.fees_usdt, result.reason)


def _sleep_to_next_close(tf_ms: int, grace_s: float = 3.0) -> None:
    """Sleep until just after the current candle closes."""
    now_ms = int(time.time() * 1000)
    next_close = (now_ms // tf_ms + 1) * tf_ms
    time.sleep(max((next_close - now_ms) / 1000 + grace_s, 0))


def main() -> None:
    from maya.run import (
        _configure_logging,
        _guard_live,
        _report_scan,
        _start_api,
        _startup_reconciliation,
    )

    settings = get_settings()
    _configure_logging(settings)
    if settings.mode is RunMode.SIM:
        logger.critical("The scanner needs live market data — use MAYA_MODE=paper "
                        "(or testnet/live).")
        sys.exit(2)
    _guard_live(settings)
    equity = _startup_reconciliation(settings)
    now_ts = int(time.time() * 1000)

    journal = TradeJournal()
    risk = RiskManager.from_journal(settings, equity, journal, now_ts)
    execution = ExecutionEngine(settings)
    agent = ScanAgent(settings, journal, Scanner(settings, risk=risk), execution, risk)

    monitor = None
    if settings.uses_real_orders:
        from maya.execution.monitor import PositionMonitor
        monitor = PositionMonitor(settings, execution, journal, risk)
        monitor.start()

    # The same dashboard/API as the single-symbol agent (off unless
    # MAYA_API_ENABLED), plus the latest scan in its snapshot.
    api = _start_api(settings, agent, journal, lambda: agent.steps)

    logger.info("=== Maya scanner === mode={} tf={} up to {} coins",
                settings.mode.value, settings.timeframe, settings.scan_max_coins)
    tf_ms = timeframe_ms(settings.timeframe)
    # The scanner polls on its own clock, so "data arrived" means a step that
    # actually read candles for at least one coin.
    from maya.data.watchdog import FeedWatchdog
    watchdog = FeedWatchdog(
        settings.timeframe,
        on_stale=lambda gap: risk.check_kill_switch(int(time.time() * 1000),
                                                    data_stale=True)).start()
    try:
        while True:
            agent.step(int(time.time() * 1000))
            if agent.got_data:
                watchdog.beat()
            if risk.account.state is SystemState.HALTED and not execution.has_position:
                logger.critical("System HALTED ({}). Stopping.",
                                ", ".join(risk.account.halt_reasons))
                break
            _sleep_to_next_close(tf_ms)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        watchdog.stop()
        if monitor is not None:
            monitor.stop()
        if api is not None:
            api.stop()
        # Same clean-shutdown rule as the single-symbol agent: flatten, so a
        # restart never finds a venue position the fresh process doesn't know.
        if execution.has_position:
            from maya.execution.engine import flatten_on_shutdown

            pos = execution.position
            price = pos.entry_price
            try:   # best effort: the coin's latest close, for honest PnL
                recent = agent.scanner.fetch_candles(pos.symbol, 1)
                if recent:
                    price = recent[-1].close
            except Exception as exc:  # noqa: BLE001
                logger.warning("no closing price for {} ({}); booking at entry", pos.symbol, exc)
            flatten_on_shutdown(execution, risk, journal, price, int(time.time() * 1000))
        _report_scan(risk, journal)
        journal.close()


if __name__ == "__main__":
    main()
