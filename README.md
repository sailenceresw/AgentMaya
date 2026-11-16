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

# Declared research hypotheses and what has been tested:
python -m maya.research list
python -m maya.research registry
```

## Implementation

A runnable **LangGraph** application implementing the 7-layer architecture. The
graph is invoked once per candle:

```
START → manage (L5) → signal (L2) → strategy (L3) → learning (L6) → risk (L4) → execution (L5) → END
```

```
maya/
  agents/      # the desk — Atlas, Echo, Flow, Sentinel + Maya's conference
  data/        # L1 ingestion — simulated, scenario & ccxt feeds + models
  signals/     # L2 order flow, volume profile, absorption, AMT phase,
               #    higher-timeframe folding, technicals, patterns
  strategy/    # L3 three setups, confluence scoring, optional LLM veto
  risk/        # L4 hard limits, kill switch, position sizing
  execution/   # L5 paper, ccxt & MetaMask brokers, position management
  fees.py      # per-venue fee model: maker/taker + MetaMask builder fee
  scanner.py   # multi-coin scanner; scan_run.py trades its best pick
  journal/     # L6 SQLite trade journal + performance metrics + lessons table
  learning/    # L6 lessons from losing trades and the veto that reads them
  research/    # hypothesis harness: holdout, registry, corrected t, costs
  timeframe.py # single source of truth for timeframe parsing
  graph.py     # LangGraph StateGraph wiring the layers
  run.py       # ingestion loop / CLI entrypoint
tests/         # indicators, risk limits, agents, end-to-end pipeline
```

## The desk

Six pipeline agents run in order each candle; four analysts sit inside Maya
and file an opinion on the same bar. Node keys are unchanged — these are names
for humans reading logs.

| Agent | Node | Duty |
|---|---|---|
| **Warden** | `manage` | Watches open positions; journals every close |
| **Iris** | `signal` | Perception — order flow, profile, ATR, HTF, patterns |
| **Maya** | `strategy` | Runs the conference and decides |
| **Mentor** | `learning` | Reads past trades in the same context; can only veto |
| **Sentinel** | `risk` | Hard limits and kill switch; absolute veto |
| **Hermes** | `execution` | Places orders; decides nothing |

Analysts: **Atlas** (support/resistance zones, structure), **Echo**
(candlestick patterns weighted by *where* they print), **Flow** (CVD,
absorption, delta), and **Sentinel** again — so risk states its case rather
than vetoing silently.

Maya declines when the desk is split: two analysts pointing opposite ways is a
reason to stand down, not to size small.

> **The desk is an analysis layer, not a signal source.** It was measured to the
> same standard as everything else and did not predict: `maya_verdict` held its
> claimed direction in only 3/8, 4/8 and 2/8 sub-periods on BTC/ETH/SOL, and no
> condition was correct-signed with `|t| > 2` and unanimous. So `desk.confer()`
> is deliberately **not** wired into the trading decision — it exists to make
> the system's reasoning readable, which it does well.

## Fees decide what is tradable

Through the MetaMask Agent Wallet a Hyperliquid round trip costs up to
**0.26%** of the position: exchange fees plus MetaMask's builder fee, on both
legs. Fees scale with size just as profit does, so only a larger move per
trade beats them. A 1m scalp aiming for 0.5% gives half its target to fees
and needs a 70% win rate to break even. A 1h swing aiming for 6% gives up 4%.
Every trade is now screened **net of fees** (net R:R ≥ 2.5, fees ≤ 20% of the
target), and the real fee figures are read from `mm perps quote` at startup.
See [the fee section](docs/RUNNING.md#fees-what-a-trade-really-costs-and-the-fee-gate).

## Multi-coin scanner

`python -m maya.scanner` reads every liquid Hyperliquid perp and runs each
through the same strategy, the chart desk and the fee gate. It ranks what's
left by net R:R after fees. `python -m maya.scan_run` trades the best
candidate (one position at a time, every hard limit intact).
`python -m maya.backtest.sweep` measures which coins and timeframes pay
after fees **before** any money is committed.

## Order flow is real

CVD comes from Binance kline field 9 (`taker_buy_base_volume`) — the exchange's
own aggressive-buy total — so `delta = 2*taker_buy - volume` exactly. This
module previously synthesised ticks from candle direction, which made every
CVD-derived signal a function of the generator rather than the market.

## Venues

Execution targets one venue per run (`MAYA_VENUE`);
the signal, strategy and risk layers are venue-agnostic and unchanged by the
choice.

| `MAYA_VENUE` | Venue | Auth | Status |
|---|---|---|---|
| `binance` (default) | Binance USD-M perps via ccxt | API key/secret | The tested path |
| `hyperliquid` | Hyperliquid perps via the MetaMask Agent Wallet `mm` CLI (v7.1) | Self-custodial wallet, no exchange keys | Unit-tested against the v7.1 CLI, **no venue hours yet** |
| `hyperliquid` with `MAYA_HL_BROKER=sdk` | Hyperliquid perps via the official Python SDK, testnet by default | API wallet key (cannot withdraw) | Unit-tested against a fake venue, **never run against the venue** |

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

## Measured performance — read this before trading it

Tested on **real order flow**, 24 months (2024-08-10 → 2026-08-09), BTC/ETH/SOL,
15m bars, split into 8 sub-periods of ~91 days.

**No signal showed predictive power.** Conditional forward returns across nine
signal conditions: nothing reached `|t| > 2` on any symbol. Backtests are
negative at every configuration tested (best case 410 trades, 22.2% win rate,
profit factor 0.38).

Two findings worth knowing before changing anything:

- **The CVD confirmation has the wrong sign.** Positive delta preceded
  *negative* forward returns in 8 of 8 sub-periods on BTC and SOL — short-term
  mean reversion. But `liquidity_sweep_choch` and `compression_breakout` both
  require `cvd_1m > 0` to confirm a **long**. `maya/agents/flow.py` encodes the
  measured reading; the legacy setups are deliberately left untouched.
- **The effect is ~1 bps against ~10 bps of round-trip taker cost**, and it is
  what makers earn and takers pay. Capturing it needs post-only limit entries,
  not market orders.

Do not tune thresholds or add indicators to fix this — nine factors have been
measured and none discriminate. The open directions are: flip the CVD sign and
re-test, move to maker execution, or change the premise entirely. The first
two are now declared as hypotheses (`cvd_flip`, `maker_cvd_reversion`) and
have not been run; see the results table below.

## Results: what has been tested since

Every new idea goes through the research harness (`python -m maya.research`),
which records each test in a registry. A hypothesis passes only with at least
100 trades, a mean return above zero after costs, a positive mean in at least
6 of 8 sub-periods, and a t-stat above a threshold that rises with every test
run. A pass then has to hold on the newest 120 days, which are read once.

| Hypothesis | Trades | Net after costs | Corrected t-stat | Holdout | Result |
|---|---|---|---|---|---|
| `cvd_follow_baseline` | | | | | not run |
| `baseline_1m_setups` | | | | | not run |
| `mtf_bias_filter` | | | | | not run |
| `cvd_flip` | | | | | not run |
| `maker_cvd_reversion` | | | | | not run |
| `trail_1r_after_2r` | | | | | not run |
| `kronos_small_15m` | | | | | not run (needs Kronos installed) |
| `mtf_15s_trigger` | | | | | blocked: needs trade-level history |

**Nothing in this table has passed, and nothing has failed, because nothing
has been run.** The harness and the hypotheses were written without access to
market data. The only measured result in this repository is still the study
above: no predictive signal, best profit factor 0.38.

Fill the table in from `python -m maya.research registry` after running each
one. A failed row stays in the table.

What was added alongside the harness, all off by default unless noted:

- Risk rules for a small account: 3% risk per trade, 10 trades a day, 10%
  daily loss limit, 10x leverage and notional cap. These are on, and three of
  them are looser than before.
- A stop that locks in breakeven plus fees at +0.6%. On. A trailing stop
  measured in the trade's own stop distance is available and off.
- A lesson saved for every losing trade, and a veto on setups whose context
  has a losing record. On.
- A 15m bias filter and a 15-second order-flow trigger.
- Two setups with the CVD confirmation reversed.
- Post-only entry simulation for paper and backtest runs.
- A Kronos forecast as one more input to the signal agent.
- A Hyperliquid testnet broker on the official SDK. Not yet run against the
  venue.

Details for each are in [`docs/RUNNING.md`](docs/RUNNING.md).

## Roadmap

Open, in the order they should happen:

1. Download the data and run the runnable hypotheses. Record every result.
2. Run `python -m maya.check_hyperliquid --smoke` on Hyperliquid testnet and
   fix whatever the first real responses show.
3. Decide whether the weekly loss limit (8%) should stay tighter than the
   daily one (10%). In simulation the daily limit never fires on a $10
   account and the weekly one halts most weeks (see the Monte Carlo section
   of `docs/RUNNING.md`).
4. Get trade-level history so `mtf_15s_trigger` can be tested.
5. Install Kronos and run `kronos_small_15m`.
6. Add a position monitor for the Hyperliquid SDK path, so exits are booked
   at the real fill price.

Not before real money:

- At least one hypothesis passes the corrected threshold and the holdout,
  after costs.
- Testnet results match the simulator within a small tolerance.
- At least four weeks of paper trading with no risk-limit breach.

If nothing passes, the premise needs changing. Tuning thresholds until
something passes is not a result.

> ⚠️ **Disclaimer:** Algorithmic trading involves substantial risk of capital loss. The code here runs and can place real orders, and nothing in it has been shown to be profitable.
