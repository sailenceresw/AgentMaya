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

## Backtesting

Before risking any capital, evaluate the strategy on historical data. The
backtester replays candles through the **same** signal → strategy → risk →
execution pipeline the live agent uses, then reports the spec's go/no-go gates
(§14.1): profit factor > 1.5, max drawdown < 20%, Sharpe > 1.0, ≥ 200 trades.

```bash
# Offline demo (no network) — proves the machinery; NOT a real edge:
python -m maya.backtest --source demo --days 250

# Real evaluation from a CSV of historical klines:
python -m maya.backtest --source csv --csv path/to/ohlcv.csv --equity 1000

# Download history from Binance and backtest (needs Binance network access):
python -m maya.backtest --source ccxt --days 365 --symbol BTCUSDT --timeframe 1m
```

CSV columns (header optional): `timestamp, open, high, low, close, volume` —
this matches Binance kline dumps from <https://data.binance.vision>.

> **Two honest caveats.** (1) The `demo` source replays one scripted winning
> setup across many days, so its glowing numbers are meaningless — it only
> proves the backtester runs. (2) Klines carry no per-trade data, so CVD is
> *approximated* from where each candle closes in its range. A true order-flow
> backtest needs tick/aggTrade data (spec §4.2); treat kline-based results as
> indicative, then confirm on paper/testnet.

## Connecting to Binance

The agent talks to **Binance USD-M Futures** via `ccxt`. Symbols are converted
to ccxt's unified format automatically (`BTCUSDT` → `BTC/USDT:USDT`), and order
amounts/prices are rounded to the exchange's filters.

**Verify the connection before trading:**

```bash
# Public market data only (no keys) — confirms you can reach Binance:
MAYA_MODE=paper python -m maya --check

# Full check including the account endpoint (needs keys):
MAYA_MODE=testnet BINANCE_API_KEY=... BINANCE_API_SECRET=... python -m maya --check
```

A healthy check prints the last price and (if keyed) your USDT equity. If it
fails, the output lists the likely cause:

1. **Restricted egress** — a proxy/firewall blocking outbound traffic. Allow
   `fapi.binance.com` and `testnet.binancefuture.com`, or run on a host with
   Binance access. (Cloud/CI environments often block these by default.)
2. **Region block** — Binance Futures is unavailable in some jurisdictions
   (e.g. the US). Use a permitted region/VPS (the spec recommends Tokyo/Singapore
   for latency).
3. **Wrong mode/keys** — testnet needs *testnet* keys; live needs
   futures-enabled keys.

### API keys

Put credentials in `.env` (copied from `.env.example`); never commit them:

```ini
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
```

Use a key with **futures permission only**, **withdrawals disabled**, and an
**IP whitelist** (spec section 11). For `testnet`, create keys at
<https://testnet.binancefuture.com>.

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
