"""LangGraph node implementations wiring the seven layers together.

Each method is a graph node: it takes the current :class:`TradingState`, calls
into the relevant layer service, and returns the state updates. The node order
encodes the spec's data flow:

    manage (L5 mgmt) -> signal (L2) -> strategy (L3) -> risk (L4) -> execute (L5)

Layer 1 (ingestion) feeds candles into the runner; Layer 6 (journal) is written
from the manage node when a trade closes; Layer 7 (monitoring) is the logging.
"""
from __future__ import annotations

import time

from loguru import logger

from maya.config import Settings
from maya.execution.engine import ExecutionEngine
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager, SystemState
from maya.signals.processor import SignalProcessor
from maya.state import TradingState
from maya.strategy.engine import StrategyEngine


class TradingNodes:
    """Holds stateful services and exposes them as LangGraph node callables."""

    def __init__(self, settings: Settings, journal: TradeJournal,
                 equity: float | None = None, now_ts: int | None = None) -> None:
        self.settings = settings
        self.processor = SignalProcessor(settings.symbol)
        self.strategy = StrategyEngine(settings)
        equity = settings.start_equity if equity is None else equity
        now_ts = int(time.time() * 1000) if now_ts is None else now_ts
        # Restores trades_today / consecutive_losses / day_start_equity from
        # the journal (spec 8.4) rather than always starting at zero — see
        # RiskManager.from_journal for why this matters for the hard limits.
        self.risk = RiskManager.from_journal(settings, equity, journal, now_ts)
        self.execution = ExecutionEngine(settings)
        self.journal = journal

    # -- nodes ------------------------------------------------------------- #
    def manage_node(self, state: TradingState) -> TradingState:
        """Advance any open position against the new candle; journal closes."""
        candle = state["candle"]
        result = self.execution.manage(candle)
        if result is not None:
            self.journal.record(result, result.position.confluences)
            self.risk.on_trade_closed(result.pnl_usdt, candle.timestamp)
            logger.info("Trade closed: {} {} pnl={:.2f} ({})",
                        result.position.setup_name, result.result,
                        result.pnl_usdt, result.reason)
            return {"closed_trade": result}
        return {"closed_trade": None}

    def signal_node(self, state: TradingState) -> TradingState:
        candle = state["candle"]
        signal = self.processor.process(candle)
        return {"signal": signal}

    def strategy_node(self, state: TradingState) -> TradingState:
        signal = state.get("signal")
        # No new entries while a position is open (single-position system).
        if signal is None or self.execution.has_position:
            return {"intent": None}
        intent = self.strategy.evaluate(signal)
        return {"intent": intent}

    def risk_node(self, state: TradingState) -> TradingState:
        candle = state["candle"]
        self.risk.check_kill_switch(candle.timestamp)
        halted = self.risk.account.state is SystemState.HALTED
        intent = state.get("intent")
        if intent is None:
            return {"risk_decision": None, "halted": halted}
        decision = self.risk.screen(intent, candle.timestamp)
        if not decision.approved:
            logger.debug("Risk rejected {}: {}", intent.setup_name, decision.reason)
        return {"risk_decision": decision, "halted": halted}

    def execution_node(self, state: TradingState) -> TradingState:
        intent = state.get("intent")
        decision = state.get("risk_decision")
        candle = state["candle"]
        if intent is None or decision is None or not decision.approved:
            return {"opened": False}
        opened = self.execution.open(intent, decision, candle.timestamp)
        if opened:
            self.risk.on_trade_opened()
        return {"opened": opened}
