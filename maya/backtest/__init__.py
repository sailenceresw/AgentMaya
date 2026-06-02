"""Backtesting (spec section 3.2.3 / Phase 2).

A custom event-driven backtester that replays historical candles through the
*same* signal/strategy/risk/execution pipeline the live agent uses, so results
reflect the real logic rather than a re-implementation.
"""
