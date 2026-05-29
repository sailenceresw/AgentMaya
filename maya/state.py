"""Shared graph state passed between LangGraph nodes.

One :class:`TradingState` flows through the graph per candle. Heavy service
objects (processor, engine, risk manager, journal) are *not* stored in state —
they live in :class:`~maya.nodes.TradingNodes` and are captured by the node
callables. State only carries the per-candle data artifacts.
"""
from __future__ import annotations

from typing import Optional, TypedDict

from maya.data.models import Candle
from maya.execution.engine import TradeResult
from maya.risk.manager import RiskDecision
from maya.signals.models import SignalObject
from maya.strategy.models import TradeIntent


class TradingState(TypedDict, total=False):
    candle: Candle
    signal: Optional[SignalObject]
    intent: Optional[TradeIntent]
    risk_decision: Optional[RiskDecision]
    closed_trade: Optional[TradeResult]
    opened: bool
    halted: bool
