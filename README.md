# AgentMaya

Autonomous AI trading agent for Binance USD-M Perpetual Futures — 1-minute order-flow scalping using a Fabervaale-style methodology (CVD, Volume Profile, absorption patterns) with strict, hard-coded risk management.

## Documentation

- [**Architecture & Technical Specification**](docs/MAYA_TRADING_SYSTEM_SPEC.md) — the full design blueprint covering all seven system layers (data ingestion, signal processing, strategy engine, risk management, execution, learning/analytics, and monitoring), technology stack, development phases, cost estimates, security requirements, and go/no-go gates.
- [**Running the agent**](docs/RUNNING.md) — install, run modes, the demo scenario, and the LangGraph wiring.

## Quick start

```bash
python -m pip install -r requirements.txt

# Run the full agent in simulation mode (no API keys, no real orders):
python -m maya

# Watch a trade flow through every layer via a scripted setup:
MAYA_SCENARIO=true MAYA_MAX_CANDLES=0 python -m maya

# Tests:
python -m pytest -q
```

## Implementation

A runnable **LangGraph** application implementing the 7-layer architecture. The
graph is invoked once per candle:

```
START → manage (L5) → signal (L2) → strategy (L3) → risk (L4) → execution (L5) → END
```

```
maya/
  data/        # L1 ingestion — simulated, scenario & ccxt feeds + models
  signals/     # L2 CVD, volume profile, absorption, divergence, AMT phase
  strategy/    # L3 three setups, confluence scoring, optional LLM veto
  risk/        # L4 hard limits, kill switch, position sizing
  execution/   # L5 paper, ccxt & MetaMask brokers, position management
  journal/     # L6 SQLite trade journal + performance metrics
  graph.py     # LangGraph StateGraph wiring the layers
  run.py       # ingestion loop / CLI entrypoint
tests/         # indicators, risk limits, end-to-end pipeline
```

## Venues

Execution can target either of two mutually exclusive venues (`MAYA_VENUE`);
the signal, strategy and risk layers are venue-agnostic and unchanged by the
choice.

| `MAYA_VENUE` | Venue | Auth | Status |
|---|---|---|---|
| `binance` (default) | Binance USD-M perps via ccxt | API key/secret | The tested path |
| `hyperliquid` | Hyperliquid perps via the MetaMask Agent Wallet `mm` CLI | Self-custodial wallet, no exchange keys | Unit-tested, **no venue hours yet** |

A MetaMask wallet cannot trade Binance and a Binance key cannot trade
Hyperliquid — selecting `hyperliquid` moves execution to a different market,
it does not connect MetaMask to Binance. Read the caveats in
[`docs/RUNNING.md`](docs/RUNNING.md#trading-hyperliquid-via-the-metamask-agent-wallet-maya_venuehyperliquid)
before using it: SL/TP is not atomic with entry, fill prices are estimated,
and the CLI was measured at 24–51s per call — long enough to be disqualifying
on the default 1-minute timeframe.

This is an **MVP**: it runs end-to-end in safe simulation mode by default. Live
trading is gated behind an explicit interlock. See
[`docs/RUNNING.md`](docs/RUNNING.md) for what is implemented vs. intentionally
minimal before live use.

> ⚠️ **Disclaimer:** Algorithmic trading involves substantial risk of capital loss. This repository contains architecture and design documentation only and does not guarantee profitability.
