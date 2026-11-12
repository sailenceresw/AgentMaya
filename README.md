# AgentMaya

Autonomous AI trading agent for **perpetual futures** — 1-minute order-flow
scalping (CVD, Volume Profile, absorption patterns) with strict, hard-coded risk
management. Trades on **Hyperliquid** (on-chain perps, default) via your wallet
and an agent key, or on **Binance USD-M Futures**.

## Documentation

- [**Trading on Hyperliquid with your wallet**](docs/HYPERLIQUID.md) — agent-wallet setup, funding, and running (start here for on-chain perps).
- [**Architecture & Technical Specification**](docs/MAYA_TRADING_SYSTEM_SPEC.md) — the full design blueprint covering all seven system layers, technology stack, development phases, security requirements, and go/no-go gates.
- [**Running the agent**](docs/RUNNING.md) — install, run modes, backtesting, Telegram control, and the LangGraph wiring.

## Quick start

```bash
python -m pip install -r requirements.txt

# Run the full agent in simulation mode (no keys, no real orders):
python -m maya

# Watch a trade flow through every layer via a scripted setup:
MAYA_SCENARIO=true MAYA_MAX_CANDLES=0 python -m maya

# Verify the venue connection (Hyperliquid by default):
MAYA_MODE=testnet python -m maya --check

# Backtest the strategy (offline demo; or --source csv for real data):
python -m maya.backtest --source demo --days 250

# Tests:
python -m pytest -q
```

### Venues

- **Hyperliquid (default, `MAYA_VENUE=hyperliquid`)** — on-chain perps. You
  trade from your wallet; the bot signs with an **agent key that cannot
  withdraw**. See [`docs/HYPERLIQUID.md`](docs/HYPERLIQUID.md).
- **Binance (`MAYA_VENUE=binance`)** — Binance USD-M Futures via `ccxt`/API key.
  See [`docs/RUNNING.md`](docs/RUNNING.md#connecting-to-binance).

Both expose the same modes: `sim` (synthetic), `paper` (real data, simulated
orders), `testnet` (sandbox), `live` (real money, gated behind an interlock).

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
  execution/   # L5 paper & ccxt brokers, position management
  journal/     # L6 SQLite trade journal + performance metrics
  backtest/    # event-driven backtester + go/no-go gates (data, engine, metrics)
  graph.py     # LangGraph StateGraph wiring the layers
  run.py       # ingestion loop / CLI entrypoint
tests/         # indicators, risk limits, Binance client, backtest, end-to-end
```

This is an **MVP**: it runs end-to-end in safe simulation mode by default. Live
trading is gated behind an explicit interlock. See
[`docs/RUNNING.md`](docs/RUNNING.md) for what is implemented vs. intentionally
minimal before live use.

> ⚠️ **Disclaimer:** Algorithmic trading involves substantial risk of capital loss. This repository contains architecture and design documentation only and does not guarantee profitability.
