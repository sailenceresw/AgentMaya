"""Builds the LangGraph StateGraph that processes one candle end-to-end.

    START -> manage -> signal -> strategy -> risk -> execution -> END

The graph is invoked once per candle by :mod:`maya.run`. Keeping the graph
single-pass (one candle per invocation) makes the data flow explicit and lets
the runner own the ingestion loop and graceful shutdown.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from maya.nodes import TradingNodes
from maya.state import TradingState


def build_graph(nodes: TradingNodes):
    g = StateGraph(TradingState)

    g.add_node("manage", nodes.manage_node)
    g.add_node("signal", nodes.signal_node)
    g.add_node("strategy", nodes.strategy_node)
    g.add_node("risk", nodes.risk_node)
    g.add_node("execution", nodes.execution_node)

    g.add_edge(START, "manage")
    g.add_edge("manage", "signal")
    g.add_edge("signal", "strategy")
    g.add_edge("strategy", "risk")
    g.add_edge("risk", "execution")
    g.add_edge("execution", END)

    return g.compile()
