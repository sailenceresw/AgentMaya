"""Event-driven backtester.

Replays historical candles through the *same* LangGraph pipeline the live agent
uses (manage -> signal -> strategy -> risk -> execution), records a
mark-to-market equity curve, and computes performance metrics + go/no-go gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from loguru import logger

from maya.backtest import metrics as M
from maya.config import Settings
from maya.data.models import Candle
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes


@dataclass
class BacktestResult:
    metrics: M.BacktestMetrics
    equity_curve: list[tuple[int, float]]


class Backtester:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.journal = TradeJournal(db_path=":memory:")
        self.nodes = TradingNodes(settings, self.journal)
        self.graph = build_graph(self.nodes)

    def run(self, candles: Iterable[Candle]) -> BacktestResult:
        start_equity = self.settings.start_equity
        equity_curve: list[tuple[int, float]] = []
        n = 0
        last_candle: Candle | None = None

        for candle in candles:
            self.graph.invoke({"candle": candle})
            acc = self.nodes.risk.account
            # Mark-to-market: realized equity + open position's unrealized PnL.
            unreal = 0.0
            if self.nodes.execution.has_position:
                unreal = self.nodes.execution.position.unrealized_pnl(candle.close)
            equity_curve.append((candle.timestamp, acc.equity + unreal))
            last_candle = candle
            n += 1

        # Flatten any position still open at the end of the data.
        if self.nodes.execution.has_position and last_candle is not None:
            result = self.nodes.execution.force_close(last_candle.close, last_candle.timestamp)
            if result:
                self.journal.record(result, result.position.confluences)
                self.nodes.risk.on_trade_closed(result.pnl_usdt, last_candle.timestamp)
                equity_curve.append((last_candle.timestamp, self.nodes.risk.account.equity))

        logger.info("Replayed {} candles", n)
        m = M.compute(self.journal.metrics(), equity_curve, start_equity)
        self.journal.close()
        return BacktestResult(metrics=m, equity_curve=equity_curve)
