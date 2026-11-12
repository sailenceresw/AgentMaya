"""LangGraph node implementations wiring the seven layers together.

Each method is a graph node: it takes the current :class:`TradingState`, calls
into the relevant layer service, and returns the state updates. The node order
encodes the spec's data flow:

    manage (L5 mgmt) -> signal (L2) -> strategy (L3) -> risk (L4) -> execute (L5)

Layer 1 (ingestion) feeds candles into the runner; Layer 6 (journal) is written
from the manage node when a trade closes; Layer 7 (monitoring) is the logging.
"""
from __future__ import annotations

from loguru import logger

from maya.config import RiskLimits, Settings
from maya.execution.engine import build_execution_engine
from maya.journal.store import TradeJournal
from maya.risk.manager import RiskManager, SystemState
from maya.signals.processor import SignalProcessor
from maya.state import TradingState
from maya.strategy.engine import StrategyEngine


def _load_news(settings: Settings):
    """Load the optional high-impact news calendar for trade blackouts (§7.4)."""
    if not settings.news_calendar:
        return None
    try:
        from maya.risk.news import NewsCalendar
        cal = NewsCalendar.from_file(settings.news_calendar,
                                     RiskLimits.NEWS_BLACKOUT_MINUTES)
        logger.info("Loaded {} news events from {}", len(cal.events),
                    settings.news_calendar)
        return cal
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load news calendar {}: {}",
                       settings.news_calendar, exc)
        return None


class TradingNodes:
    """Holds stateful services and exposes them as LangGraph node callables."""

    def __init__(self, settings: Settings, journal: TradeJournal,
                 notifier=None) -> None:
        self.settings = settings
        self.processor = SignalProcessor(settings.symbol)
        self.strategy = StrategyEngine(settings)
        self.risk = RiskManager(settings, settings.start_equity,
                                news_calendar=_load_news(settings))
        self.execution = build_execution_engine(settings)
        self.journal = journal
        self.notifier = notifier            # TelegramBot or None (no-op)
        self._was_halted = False

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
            if self.notifier:
                self.notifier.trade_closed(result, self.risk.account.equity)
            return {"closed_trade": result}
        return {"closed_trade": None}

    def signal_node(self, state: TradingState) -> TradingState:
        candle = state["candle"]
        signal = self.processor.process(candle)
        return {"signal": signal}

    def strategy_node(self, state: TradingState) -> TradingState:
        signal = state.get("signal")
        if signal is None:
            return {"intent": None}
        # Always update rolling swing context, even while a position is open.
        ctx = self.strategy.observe(signal)
        # No new entries while a position is open (single-position system).
        if self.execution.has_position:
            return {"intent": None}
        intent = self.strategy.evaluate(signal, ctx)
        return {"intent": intent}

    def risk_node(self, state: TradingState) -> TradingState:
        candle = state["candle"]
        self.risk.roll_periods(candle.timestamp)
        self.risk.check_kill_switch(candle.timestamp)
        halted = self.risk.account.state is SystemState.HALTED
        if halted and not self._was_halted and self.notifier:
            self.notifier.halted(self.risk.account.halt_reasons)
        self._was_halted = halted
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
            if self.notifier:
                self.notifier.trade_opened(self.execution.position)
        return {"opened": opened}
