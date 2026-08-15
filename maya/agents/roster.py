"""Who's who on the desk.

Two tiers, because they do genuinely different jobs:

**Pipeline agents** are the five LangGraph nodes — the assembly line that runs
once per candle. Each owns a stage and hands the next one its output.

**Analysts** are the specialists inside Maya. They do not run in sequence; they
all look at the same bar and each files an opinion, which Maya weighs.

The graph's node *keys* stay as they were (``manage``, ``signal``, ...) so
existing state, tests and tooling keep working — these names are the display
layer, used in logs and journals where a human is reading.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRole:
    name: str
    node: str           # graph node key, or '' for analysts
    layer: str
    duty: str


PIPELINE: tuple[AgentRole, ...] = (
    AgentRole("Warden", "manage", "L5 position mgmt",
              "Watches open positions: stops, targets, max duration, and "
              "journals every close."),
    AgentRole("Iris", "signal", "L2 signals",
              "Perception. Turns raw candles into a read of the market — "
              "order flow, volume profile, ATR, higher timeframes, patterns."),
    AgentRole("Maya", "strategy", "L3 strategy",
              "The desk. Runs the conference, weighs the analysts, and decides "
              "whether there is a trade."),
    AgentRole("Sentinel", "risk", "L4 risk",
              "Hard limits and the kill switch. Holds an absolute veto over "
              "everything Maya proposes."),
    AgentRole("Hermes", "execution", "L5 execution",
              "Places and cancels orders at the venue. Never decides anything "
              "— only carries out what survived Sentinel."),
)

ANALYSTS: tuple[AgentRole, ...] = (
    AgentRole("Atlas", "", "structure",
              "Support and resistance zones scored by touches, volume and "
              "recency; market structure."),
    AgentRole("Echo", "", "patterns",
              "Candlestick patterns, weighted by where they print — a hammer "
              "at support is not a hammer mid-range."),
    AgentRole("Flow", "", "order flow",
              "CVD, delta, absorption and volume profile — what aggressive "
              "traders are actually doing."),
    AgentRole("Sentinel", "", "risk",
              "Also speaks in the conference: says plainly when the account "
              "cannot take another trade, instead of vetoing silently."),
)

BY_NAME = {r.name: r for r in PIPELINE + ANALYSTS}
NODE_TO_NAME = {r.node: r.name for r in PIPELINE if r.node}


def describe() -> str:
    """Human-readable org chart, for `--roster` and the docs."""
    lines = ["Pipeline (runs in order, once per candle):"]
    for r in PIPELINE:
        lines.append(f"  {r.name:<9} [{r.node:<9}] {r.layer:<18} {r.duty}")
    lines.append("")
    lines.append("Analysts (all read the same bar, Maya weighs them):")
    for r in ANALYSTS:
        lines.append(f"  {r.name:<9} {'':<12}{r.layer:<18} {r.duty}")
    return "\n".join(lines)
