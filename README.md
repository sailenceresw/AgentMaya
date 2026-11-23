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
| `hyperliquid` with `MAYA_HL_BROKER=sdk` | Hyperliquid perps via the official Python SDK, testnet by default | API wallet key (cannot withdraw) | Run on testnet (Nov 2026, 14 trades; bugs found and fixed, see below), **never run on mainnet** |

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
two were later declared as hypotheses (`cvd_flip`, `maker_cvd_reversion`);
both failed (see below).

## Results: what has been tested since

Every new idea goes through the research harness (`python -m maya.research`),
which records each test in a registry. A hypothesis passes only with:

- at least 100 trades,
- a mean return above zero after costs,
- a positive mean in at least 6 of 8 sub-periods,
- a t-stat above a threshold that rises with every test run,
- since 2026-11-19, a return that still beats simply holding the coin after
  taking out its drift.

A pass then has to hold on the newest 120 days, which are read once.

Run November 2026 on Binance USD-M klines, taker costs (10 bps round trip).
Unless noted, the data is BTC, ETH and SOL from 2024-08-10 to 2026-08-09. In
the table, "t" is the t-stat and "needed" is the corrected threshold at the
time of the test.

| Hypothesis | Trades | Net per trade | t (needed) | Holdout | Result |
|---|---|---|---|---|---|
| `cvd_follow_baseline` (1m) | | −10.27 bps | −117.99 (2.00) | – | **fail** |
| `baseline_1m_setups` | | −10.20 bps | −10.63 (2.24) | – | **fail** |
| `mtf_bias_filter` | | −11.79 bps | −5.23 (2.24) | – | **fail** |
| `trail_1r_after_2r` | | −9.46 bps | −13.62 (2.24) | – | **fail** |
| `cvd_flip` (15m) | | −11.16 bps | −1.70 (2.24) | – | **fail** |
| `maker_cvd_reversion` (15m, maker) | | −9.45 bps | −1.37 (2.24) | – | **fail** |
| `chart_breakout_15m` | | −9.58 bps | −16.26 (2.50) | – | **fail** |
| `kronos_small_15m` | 5,925 | −10.47 bps (gross −0.47) | −10.14 | – | **fail** |
| `baseline_1h_setups` | 24 | −15.56 bps | −0.64 (2.73) | – | **fail** (too few trades) |
| `baseline_1h_48h_hold` (15 coins, 48h hold in backtest only) | 132 | −8.36 bps | −0.30 (2.77) | – | **fail** |
| `trend_30d_hold_7d` (daily, 15 coins) | 1,140 | +51.90 bps | +1.13 (2.81) | – | **fail** |
| `trend_30d_hold_7d_2020_2024` (same rules, 2020-01 to 2024-08) | 2,246 | +203.05 bps | +4.78 (2.84) | **−127.57 bps, t −1.38** | **failed the holdout** |
| `funding_extreme_reversal` (8h, 15 coins, 2020-01 to 2026-11, real funding) | 1,760 | −25.72 bps | −0.88 (2.89) | – | **fail** |
| `funding_negative_long` (its long half, declared after the split) | 1,141 | +98.98 bps | +3.14 (2.91) | – | **fail**: only +36.5 bps beyond buy-and-hold, t 1.16 |
| `trend_30d_hold_7d_pit` (point-in-time top 15, dead coins included, 2021-01 to 2026-11) | 4,256 | +88.79 bps | +3.66 (2.94) | – | **fail**: positive in 5 of 8 periods |
| `funding_extreme_reversal_pit` (same coins) | 1,969 | −24.96 bps | −0.88 (2.96) | – | **fail** |
| `mtf_15s_trigger` | | | | | blocked: needs trade-level history |

`kronos_small_15m` has no "needed" figure because it ran for about 36 hours.
It took its threshold (2.24) when it started, before later tests raised the
bar. The fail does not depend on that.

**Nothing has passed.** Notes on the results:

- **The 1m and 15m ideas lose about the round trip.** Gross is about 0 bps
  and net about −10 bps, so the fee is the whole result. On the 1m setups the
  median stop is 12.5 bps, so fees took most of the risk.
- **The 1h setups and the 48h hold were a false lead.** A diagnosis of the
  24-trade 1h run suggested the 4h hold limit cut winners short. The same
  rules with a 48h hold, on 132 trades, won 9.8% of the time and lost money.
- **Trend following passed research on 2020–2024 and then failed its
  holdout.** The 2020–2024 pass cleared every gate, including buy-and-hold
  (+198 bps after drift; t 3.0 with one averaged return per week, so coins
  that move together count once). The four months right after lost 1.28% a
  trade. The two years after that were positive but not significant. The
  pass came largely from the 2021 bull run, and the coins are 2026's
  survivors.

- **Fading funding extremes failed.** Shorting crowded longs lost 2.3% a
  trade, so the crowded side tended to keep winning. Buying crowded shorts made
  +0.9% (t 2.89 on that half alone). That split was read after the test
  failed, so it was declared as `funding_negative_long` and tested. It cleared
  the first four gates (+99 bps, t 3.14, 7 of 8 periods). It failed gate 5:
  about two thirds of the gain was the coins rising, so beyond holding it was
  +36.5 bps, t 1.16. The holdout (2026-07-18 onward) stays unread.

- **The survivor coin list hid what shorts could do.** The rows marked
  `_pit` rerun the trend and funding tests unchanged on a point-in-time coin
  list. Each year's coins are the top 15 USD-M perps by volume the December
  before, from the Binance archive, including coins that later died (LUNA,
  FTM, MATIC). Zero-volume bars the API serves after a delisting were
  dropped.
  - Trend following cleared four of five gates: +88.8 bps, t 3.66, +106.6 bps
    beyond holding at t 4.39. It failed on consistency (5 of 8 periods).
    Averaged per week, its edge over holding is t 2.50, under the 2.94 bar.
  - Read after the result: its shorts timed downtrends well (+182 bps beyond
    holding), while its longs added nothing beyond holding (+10 bps).
  - Funding extremes still failed on these coins.

The buy-and-hold gate was added because a long-biased rule on coins that
rose can clear the first four gates without predicting anything. A failed
row stays in the table. Blank trade counts weren't kept in the registry; the
other figures come from `python -m maya.research registry`.

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
- Chart drawings: trendlines, channels, triangles, wedges, double tops and
  bottoms, head and shoulders, flags, Fibonacci and breakouts. They're drawn
  on the dashboard and by `python -m maya.chart`. They're readings only, and
  nothing trades on them until `chart_breakout_15m` passes.
- A Hyperliquid testnet broker on the official SDK. Run against testnet in
  November 2026:
  - the smoke test placed and cancelled an order;
  - the multi-coin scanner opened and closed trades by itself, with market
    entries and stops and targets resting at the venue.

  A PUMP stop filled about 38 bps worse than the simulator assumed.

  **Testnet run, 18–22 Nov 2026.** The venue's own fill history shows −$30
  after fees, not the profit the journal showed. Two HYPE shorts filled at
  $71.08 in a nearly empty testnet book while the real price was about $61.5.
  Every close was then refused, yet the broker cancelled the stop and target,
  and the engine journaled the trades as closed wins. The unprotected short
  was liquidated on 21 Nov for −$28.6. The exit price logged was also the
  wrong fill (the entry, or an older trade's).

  Fixed, with regression tests in `tests/test_hyperliquid_sdk_broker.py`:
  - a refused close keeps both orders and the engine keeps the position;
  - exits are read from that trade's own fills;
  - no entry while the venue already holds the coin;
  - no entry unless the book can take the size on both sides within the
    slippage cap.

Details for each are in [`docs/RUNNING.md`](docs/RUNNING.md).

## Machine-learning experiments (`ml/`): no edge survived

A separate question from the agent: can an ML model predict BTC direction
well enough to beat holding BTC after fees? These scripts are outside the
`maya` package and the agent doesn't use them. Run in November 2026.

**Method, the same for every run.** Rules and hyperparameters were written
into each script's docstring before it was run. Monthly walk-forward:
each month is predicted only by models trained on earlier data, with the
last H hours before it purged. Decisions don't overlap. Taker fee 0.05% per
side plus funding paid. Each run has a shuffled-label control, which came
out at about 0.50 AUC every time, so nothing leaks. Models also had to beat
10,000 random shuffles of their own positions, with a Bonferroni correction
for the 20 attempts. **Jan–Nov 2026 was held out and read once at the end.**

Data:

- Binance BTCUSDT perp, hourly, 2023–2026. Includes real taker order flow
  (CVD) and funding.
- A user-supplied 1-minute BTC/USD spot file covering 2012–2026, resampled to
  hourly bars. It has no order flow and no funding, so those features were
  dropped, not reconstructed.

| Experiment | Development result | 2026 holdout | Verdict |
|---|---|---|---|
| Daily Yahoo OHLCV, 2018–2023 (`btc_daily_model.py`) | accuracy not better than the naive guess (p 0.30 / 0.47) | – | no edge |
| Hourly perp, 4h hold (`btc_hourly_orderflow.py`) | AUC 0.55, gross +74%, **net −57%** (fees 139%) | – | edge smaller than fees |
| Hourly perp, 24h hold, logistic regression (the pre-chosen primary) | **+173%** vs buy & hold +75%, beat both nulls (p 0.02 / 0.004) | **−43%** vs buy & hold −29%, AUC 0.457 | **failed** |
| 7-model zoo on perp: logit, boosted trees, random forest, extra trees, MLP, ensemble, return regression (`model_zoo.py`) | best +172% (MLP), none pass the corrected timing test | – | no pass |
| Price-only, 24h hold, user data 2014–2025 (`btc_longhistory_price.py`) | boosted trees AUC 0.535, +56,190% vs buy & hold +16,530% | **−18%** vs buy & hold −26%, AUC 0.474 | **failed** |
| Same zoo on user data | 5 of 7 pass the corrected timing test | all 5 fail timing (p 0.12–0.78), AUCs 0.49–0.53 | **failed** |
| CNN and LSTM on raw hourly sequences (`sequence_models.py`) | perp AUC 0.49; user data AUC 0.51–0.52, no pass | – | no edge |

What this shows:

- **The development-period wins were luck plus selection.** Several models
  passed strict statistical tests on 2014–2025 and then predicted 2026 no
  better than a coin flip.
- **Complexity didn't help.** Neural nets, CNN and LSTM never beat logistic
  regression or shallow trees, even with 15 years of hourly bars.
- **The edge has decayed.** AUC was about 0.55–0.6 on 2014–2019 data and
  about 0.51–0.54 from 2020. Most of the huge long-history returns come from
  years when there was no perp to trade, shorting was hard and spot fees were
  5–10x the 0.05% assumed here.
- **The 2026 holdout is now spent.** A new idea needs data from December 2026
  onward as its holdout, with its rules fixed before that data is read.
  Tuning these models further on 2014–2026 would only find more false
  winners. Different data might change the answer (open interest,
  liquidations, on-chain flows, options); different models on the same data
  won't.

Outputs (reports, predictions, equity charts) are in `ml/output_*`.

## Roadmap

Open, in the order they should happen:

1. Change the premise. Twelve tests on OHLCV candles and one on funding found nothing (see the
   results table). The next ideas need different data, such as order-book
   depth, liquidations or open interest, with their rules
   fixed before that data is read.
2. Compare testnet fills with the simulator over more trades. The first
   stops slipped on thin coins.
3. Decide whether the weekly loss limit (8%) should stay tighter than the
   daily one (10%). In simulation the daily limit never fires on a $10
   account and the weekly one halts most weeks (see the Monte Carlo section
   of `docs/RUNNING.md`).
4. Get trade-level history so `mtf_15s_trigger` can be tested.
5. Run the Hyperliquid SDK position monitor and fill-price booking on
   testnet. They're built and unit-tested, but haven't been run on the venue yet.

Not before real money:

- At least one hypothesis passes the corrected threshold and the holdout,
  after costs.
- Testnet results match the simulator within a small tolerance.
- At least four weeks of paper trading with no risk-limit breach.

If nothing passes, the premise needs changing. Tuning thresholds until
something passes is not a result.

> ⚠️ **Disclaimer:** Algorithmic trading involves substantial risk of capital loss. The code here runs and can place real orders, and nothing in it has been shown to be profitable.
