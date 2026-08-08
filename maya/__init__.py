"""Maya Trading System — autonomous order-flow scalping agent on LangGraph.

This package implements the 7-layer architecture described in
``docs/MAYA_TRADING_SYSTEM_SPEC.md`` as a runnable LangGraph application.

Layers map to LangGraph nodes:

    ingest  (L1) -> signals (L2) -> strategy (L3) -> risk (L4)
            -> execution (L5) -> journal (L6)

Layer 7 (monitoring) is surfaced through structured logging and the journal.

The default run mode is ``sim`` — a fully self-contained simulation that
requires no API keys and places no real orders.
"""

__version__ = "0.1.0"
