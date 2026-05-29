# Running the Maya Agent

This is the runnable LangGraph implementation of the architecture in
[`MAYA_TRADING_SYSTEM_SPEC.md`](MAYA_TRADING_SYSTEM_SPEC.md). It implements the
seven layers as a LangGraph state graph and runs end-to-end.

> ⚠️ **Read this first.** This is an MVP, not a production money-ready bot. The
> spec itself estimates 6–9 months to a scaled live system. The defaults here
> are deliberately safe: **simulation mode, no API keys, no real orders.** Live
> trading is gated behind an explicit interlock. Never point this at real funds
> until you have backtested and paper-traded a *proven* edge.

## Install

```bash
python -m pip install -r requirements.txt
```

(`ccxt` and `anthropic` are only imported when you actually use real-exchange or
LLM modes, so simulation runs even without them.)

## Quick start (simulation, no keys)

```bash
python -m maya
```

Runs the full pipeline on synthetic order-flow data and prints a performance
summary. Because the strategy uses conservative gates (1:4–1:5 R:R, confluence
≥ 7), it will often take **no trades** on random noise — that is realistic
behaviour for a selective order-flow system, not a bug.

## See a trade flow through every layer (demo)

```bash
MAYA_SCENARIO=true MAYA_MAX_CANDLES=0 python -m maya
```

This replays a scripted, textbook **liquidity-sweep** setup through the
*unmodified* strategy and risk logic so you can watch the
signal → strategy → risk → execution → journal path end-to-end:

```
Trade intent: short liquidity_sweep_choch @ 66800.00 score=7 (...)
[PAPER] OPEN  short BTCUSDT entry=66800 SL=67388 TP=63700
[PAPER] CLOSE short BTCUSDT @ 63700 (take_profit)
Trade closed: liquidity_sweep_choch win pnl=25.77
```

## Run modes

Set `MAYA_MODE` (or edit `.env`, copied from `.env.example`):

| Mode | Data | Orders | Keys needed |
|---|---|---|---|
| `sim` (default) | synthetic | simulated | none |
| `paper` | real Binance | simulated | Binance (read) |
| `testnet` | Binance testnet | real sandbox orders | Binance testnet |
| `live` | real Binance | **REAL MONEY** | Binance + interlock |

`live` mode refuses to start unless `MAYA_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`
and API credentials are present. Use a key with **futures permission only, no
withdrawals, IP-whitelisted** (spec section 11).

## Optional LLM veto layer

```bash
export MAYA_LLM_ENABLED=true
export ANTHROPIC_API_KEY=sk-ant-...
```

When enabled, an LLM reviews each trade the rules already approved and may
**veto** it (it can never originate a trade — spec section 6.4).

## Tests

```bash
python -m pytest -q
```

Covers the order-flow indicators, the risk limits/kill-switch, and an
end-to-end integration run through the LangGraph pipeline.

## How the graph is wired

`maya/graph.py` builds a `StateGraph` invoked once per candle:

```
START → manage (L5) → signal (L2) → strategy (L3) → risk (L4) → execution (L5) → END
```

- **manage** advances any open position against the new candle (SL/TP,
  breakeven, max-duration) and writes closed trades to the journal (L6).
- **signal** computes CVD, volume profile, absorption, divergence, AMT phase.
- **strategy** runs the three setups, scores confluence, applies the LLM veto.
- **risk** runs the kill switch and screens the intent against the hard limits.
- **execution** opens approved trades via the paper or ccxt broker.

The ingestion loop (L1) lives in `maya/run.py`; monitoring (L7) is the
structured logging plus the journal metrics.

## What's implemented vs. stubbed

**Implemented & runnable:** all seven layers wired through LangGraph, the three
setups, confluence scoring, the full hard-limit risk gate + kill switch,
position sizing, paper execution with SL/TP/breakeven/duration management, the
SQLite trade journal + metrics, the optional LLM veto, and a ccxt broker/feed
for paper/testnet/live.

**Intentionally minimal (extend before live use):** the ccxt feed polls klines
rather than using the websocket streams in spec §4.2; funding rate / OI /
long-short ratio and the news-blackout calendar are placeholders; there is no
event-driven backtester yet; Telegram/dashboard (L7 UI) is logging-only;
state reconciliation on restart (spec §8.4) is not yet implemented.
