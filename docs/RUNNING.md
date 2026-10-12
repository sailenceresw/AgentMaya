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

## Connecting to Binance Futures testnet

1. Log in to https://testnet.binancefuture.com with GitHub.
2. Generate an API key/secret under API Management, with Futures permission.
3. Put them in `.env`:
   ```
   MAYA_MODE=testnet
   BINANCE_API_KEY=...
   BINANCE_API_SECRET=...
   ```
4. Before running the full agent, sanity-check the connection on its own —
   this makes **no trades**, it only reads:
   ```bash
   python -m maya.check_connection
   ```
   It verifies your keys, clock sync, USDT balance, symbol price, one-way vs.
   hedge position mode, and whether the exchange already has an open
   position/order you don't know about. Fix anything it flags before running
   the agent itself.
5. `python -m maya` now runs the same check automatically at startup for
   `paper`/`testnet`/`live`, and **refuses to start** if it fails or if the
   exchange shows state Maya has no local record of (spec section 8.4 — never
   auto-resolve a mismatch, always require a human to look at it).

## Trading Hyperliquid via the MetaMask Agent Wallet (`MAYA_VENUE=hyperliquid`)

Maya can route orders through a **self-custodial MetaMask agent wallet**
instead of a Binance API key. Understand the substitution first:

> **A MetaMask wallet cannot trade Binance.** Binance is a centralized venue
> authenticated with an API key/secret against a Binance-held balance; a
> MetaMask wallet is on-chain and self-custodial. Selecting this venue does
> not "connect MetaMask to Binance" — it moves execution to **Hyperliquid
> perpetuals**, which is what the MetaMask Agent Wallet CLI actually
> supports. Layers 2-4 (signals, setups, confluence, risk) are unchanged and
> venue-agnostic; only the broker swaps.

### Setup

```bash
npm install -g @metamask/agent-wallet@latest   # provides the `mm` CLI (v7.1+, Node 22.18+)
npx skills add metamask/agent-skills           # agent-facing skill docs
mm doctor                                      # CLI version + session health

mm login                                       # browser / QR — interactive, do this yourself
mm init --wallet server-wallet --mode guard    # server-held keys + Guard policy
mm wallet create --name maya-agent --trading-mode guard
```

**Funding is a two-step process, and the first step is off-CLI.**
`mm perps deposit` bridges USDC *from the wallet's on-chain balance* into the
Hyperliquid sub-account — it cannot conjure funds. A newly created wallet
reports `{"totalValue": "0", "chains": []}`, so the deposit fails with nothing
to send. You must first get testnet USDC onto the wallet address on **Arbitrum
Sepolia** (chain `eip155:421614`, the testnet default for `--source-chain-id`)
from a faucet, then:

```bash
mm wallet address                                     # confirm the destination
mm wallet balance                                     # must be non-zero first
mm perps deposit --amount <USDC> --network testnet --dry-run   # preview
mm perps deposit --amount <USDC> --network testnet             # approve on mobile
```

`server-wallet` mode keeps the private keys on MetaMask's side, so **there is
no seed phrase** — that is the design. Only `--wallet byok` has a local BIP-39
mnemonic.

### Guard mode vs. an unattended bot

A freshly created Guard-mode wallet comes with `outflow_limits_usd:
rolling_24h: 0` and empty allow/block lists, and MetaMask's docs state you are
"prompted to set the limit on your first transaction". Guard mode requires
human approval for malicious transactions, risky contracts, anything outside
the allowlist, **and for raising the outflow limit** — delivered as a MetaMask
Mobile push notification or an email link.

That is a real conflict with an autonomous agent: Maya has no way to answer a
push notification at 3am, and MetaMask's documentation does not describe an
unattended-approval path. In practice:

- The **first** `mm perps deposit` will need you to approve it by hand and set
  an outflow limit. That is a one-time, human-in-the-loop step — fine.
- If a later order trips a policy rule, it will **block until approved**, and
  from Maya's side that looks like a very slow call and then a timeout with
  order state *unknown*.

Beast mode removes the allowlist friction (malicious transactions are still
blocked) at the cost of the Guard protections. Neither choice is obviously
right: Guard risks stalled orders, Beast risks unreviewed ones. Decide
deliberately rather than by default, and prefer Guard while on testnet where a
stall costs nothing.

Then:

```bash
MAYA_MODE=testnet MAYA_VENUE=hyperliquid python -m maya.check_metamask
MAYA_MODE=testnet MAYA_VENUE=hyperliquid python -m maya
```

`check_metamask.py` is the Hyperliquid counterpart to `check_connection.py`:
read-only, verifies the CLI is installed and authenticated, that the
sub-account has collateral, and that the venue shows no position/order Maya
has no record of (the same spec §8.4 gate). `python -m maya` runs it
automatically at startup and refuses to start if it fails.

### What is proven, and what is not

The command surface (`mm perps open/close/modify/positions/balance`, the
`--json` envelope, `--network testnet`) was read from `mm --help --json`
against CLI v6.0.0, and `tests/test_metamask_broker.py` pins the argv Maya
builds and its failure handling (53 tests as of the v7.1 update).

Every payload the broker parses has now been captured verbatim from a live
authenticated testnet session and is pinned as a fixture in
`tests/test_metamask_broker.py`. **Doing that found a real bug**: the balance
parser was looking for `accountValue`/`equity`/`totalEquity`/`withdrawable`/
`available`/`balance`, and the CLI emits none of them — the real field is
`totalBalance`. It would have returned `0.0` on a funded account and seeded
the risk manager with a $0 equity, silently. Confirmed shapes:

```json
perps balance   {"venue":"hyperliquid","totalBalance":"0","spendableBalance":"0.0",
                 "withdrawableBalance":"0.0","marginUsed":"0","unrealizedPnl":"0.00", ...}
perps positions []            // a BARE array, not {"positions": [...]}
perps orders    []
perps markets   [{"symbol":"BTC","maxLeverage":40,"sizeDecimals":5,
                  "markPrice":"64849.0","fundingRate":"-0.0000693105", ...}]
```

Every numeric field arrives as a **string**. Two things follow from the market
payload: `sizeDecimals` is now read from the venue instead of assumed (cached
per symbol, so it stays off the per-order path), and `maxLeverage: 40` confirms
20x is within Hyperliquid's limit for BTC.

**Order construction is venue-validated.** `mm perps open --dry-run` with
exactly the argv the broker builds returned:

```json
{"venue":"hyperliquid","symbol":"BTC","status":"prepared",
 "averagePrice":"66800","filledSize":"0.00599"}
```

`mm perps quote` on the same order independently confirms the sizing math —
`notional: "400.13"` for $20 of margin at 20x — and reports
`estimatedLiquidationPrice: "70140"`, a 5.0% adverse move against a 0.70% stop.

**Fee rate corrected.** The quote reports `feeRate: 0.00015` — Hyperliquid
charges 1.5bps for a limit order, while the engine hardcoded the Binance taker
rate of 5bps, overstating fees 3.3x on every Hyperliquid PnL calculation. Fees
are now venue-resolved (`Settings.effective_fee_rate`, overridable with
`MAYA_FEE_RATE`). Funding is also charged **hourly** on Hyperliquid, not
8-hourly as the simulator assumed.

### CLI v7.1 update (October 2026)

The broker was re-checked against `@metamask/agent-wallet` **v7.1.0**, by
reading the per-command `mm <cmd> --help --json` output, the CLI's changelog,
and its result-mapping code, and by running the CLI signed out. No perps
command or flag Maya uses changed between v6.0.0 and v7.1.0. The re-check
still found six problems in how Maya drove the CLI:

1. **A resting limit entry was left on the book with no stop.** With the
   default `MAYA_PERPS_ORDER_TYPE=limit`, an entry that does not cross the
   book comes back `status: "resting"`, which means no position exists yet.
   The follow-up `perps modify --sl --tp` then failed, the "unwind" `close`
   found nothing to close, and the limit order **stayed live**. If it filled
   later, it was a naked position Maya didn't know about. The broker now reads
   `status` (`filled` / `resting` / `submitted` / `rejected`). For a
   non-filled entry it polls positions for `MAYA_PERPS_LIMIT_FILL_TIMEOUT_S`,
   then cancels by `orderId` (`mm perps cancel`, new in this integration), then
   reads positions once more. A fill that landed during the cancel, whole or
   partial, gets its SL/TP at its real size. If the cancel fails, a `CRITICAL`
   log names the order id to cancel by hand.
2. **`ok: true` with `status: "rejected"` was counted as a success.** The
   venue can reject an order the wallet signed. Maya would then have tried to
   attach SL/TP and recorded a phantom position. It now returns `False`.
3. **Errors were read from the wrong stream.** Observed on v7.1.0: failures
   print the `{"ok": false, "error": {"code", "message", "hint"}}` envelope to
   **stderr** and leave stdout empty. Maya only parsed stdout, so every error
   reached the log as "produced no output". `MMError` now carries the CLI's
   `code` (`AUTH_FAILED`, `INSUFFICIENT_FUNDS`, ...) and its hint.
4. **Stream output broke parsing.** Once a command emits anything mid-run,
   for example a `{"_notice": {"kind": "AWAITING_MFA"}}` line while a Guard
   Mode approval is pending, the CLI switches to one JSON object per line,
   ending in `{"_summary": ...}` or `{"_error": ...}`. Maya parsed stdout as a
   single document, so an order that **succeeded** after an approval was
   reported as "non-JSON output". Both forms are now handled, and MFA
   notices are logged with their polling id.
5. **Telemetry made calls hang.** The CLI sends Segment/Sentry analytics
   *after* printing its result. With analytics enabled and its endpoint
   unreachable, `mm perps balance` printed its answer and then **hung for
   over 100s** until killed. With `MM_TELEMETRY_DISABLED=1` the identical
   call took **1.7s**. Maya now disables telemetry for the `mm` subprocess
   (`MAYA_MM_TELEMETRY=true` re-enables it). Separately, a call that times
   out *after* printing a conclusive result now uses that result instead of
   reporting the order state as unknown.
6. **No wallet-job timeout was passed.** Signing commands (open, close,
   modify, cancel) now pass `--wallet-timeout` (`MAYA_MM_WALLET_TIMEOUT_S`,
   default 90, CLI cap 600). The CLI therefore gives up on a stalled approval
   and says so before Maya kills the process. Settings refuse a
   `MAYA_MM_TIMEOUT_S` that is not longer than the wallet timeout.

New uses of the CLI:

- **Real fills in the journal.** The engine records the venue's
  `averagePrice` and `filledSize` instead of the planned entry and size.
  Market-order slippage and partial limit fills now show up in PnL and R.
- **`mm doctor` preflight.** `check_metamask` runs it first. It needs no
  session and reports the CLI version (warns below v7), `authenticated`, and
  `initialized`. MetaMask's own docs say to confirm both before any wallet
  operation. CLIs without `doctor` fall back to `mm auth status`.
- **Trading mode.** `mm wallet trading-mode get` is logged at startup, and
  running in Beast Mode on mainnet raises a warning.
- **MetaMask Pulse (optional).** `MAYA_PULSE_ENABLED=true` passes
  `mm pulse asset --hl-perps-market <coin>`, MetaMask's AI-written market
  summary, to the LLM veto as background context. It is cached for
  `MAYA_PULSE_TTL_S`, including failures, so the slow call stays off the
  per-candle path. It is used only when `MAYA_LLM_ENABLED=true`. It is
  context, not a signal: nothing here measures it as predictive.

One addition is venue-agnostic: the risk manager now **rejects a trade whose
stop sits beyond the estimated liquidation price**. Such a stop never fires,
because the venue liquidates first and keeps the whole margin. That is the
one way a trade could lose more than its sized risk. The estimate is
`1/leverage − 1.25%` maintenance margin (Hyperliquid's BTC figure, the more
conservative of the two venues), and the stop must sit within 80% of that
distance. At the default 20x, stops wider than 3% are refused.

What is **still** unverified, and cannot be until the sub-account holds funds:

- **A signed, filled order.** `--dry-run` validates and prepares; nothing has
  been signed or filled through Maya. The `filled`/`resting` result shapes and
  the populated position row (`symbol`, `side`, absolute `size`,
  `entryPrice`, `liquidationPrice`) now come from the v7.1.0 CLI source
  rather than guesswork. They have still never been observed live. The test
  fixtures built from them are labelled as such.
- **`mm wallet trading-mode get` and `mm pulse`** were only seen returning
  `AUTH_FAILED` (signed out). Their success shapes come from the source, and
  both are informational only.

Treat it as `RUNNING.md` treats the WebSocket feed: verified logic, minimal
venue hours. Testnet first.

### Caveats that differ from the Binance path

- **SL/TP is not atomic with entry.** `mm perps open` has no stop-loss or
  take-profit flag (still true in v7.1); protection is attached by a second
  `mm perps modify` call, and only once a position exists — a resting limit
  entry is waited on, then cancelled if it has not filled. Between the two the position is live and unprotected. The broker
  treats a failed attach as fatal and immediately closes the position — but
  if *that* close also fails it logs `CRITICAL` and you have a naked
  position to handle by hand. This is strictly weaker than the Binance path,
  where the stop goes on in the same burst as the entry.
- **Exit prices are estimated.** The `mm` CLI exposes no fills/trade-history
  command, so when the venue fills an SL/TP the real exit price and fee
  cannot be read back. `PositionMonitor` journals the SL/TP level instead
  and logs a warning. **Journal PnL on this venue is approximate** —
  reconcile against the Hyperliquid UI before trusting the metrics.
- **Market data still comes from Binance.** The data layer polls Binance's
  public klines endpoint (no keys needed) while orders execute on
  Hyperliquid. The two BTC prices track closely but are not identical, so
  entries and stops computed off Binance candles carry a small basis error
  against the venue actually filling them. On 1m scalping with tight stops
  that is not negligible.
- **Latency — measured, and it is disqualifying at 1m.** `mm auth status`
  (the cheapest possible command: unauthenticated, no signing, no order) was
  timed at **24s, 38s and 51s** over three consecutive subprocess calls on
  this machine. An actual `mm perps open` adds wallet signing and venue
  round-trips, and opening a Maya position costs *two* calls (open, then
  modify to attach SL/TP). That is comfortably longer than the 1-minute
  candle the decision was made on — the order lands after the setup it was
  based on has already expired. Guard mode can block on human approval on
  top of that. `MAYA_MM_TIMEOUT_S` (default 120) bounds the wait, and a
  timeout leaves order state genuinely *unknown* and is logged as such; the
  broker also logs a warning for any call over 10s.

  *Update (v7.1):* some of that latency was probably the CLI's analytics
  flush rather than the work itself. With telemetry disabled (now Maya's
  default) a signed-out call returned in 1.7s, against a 100s+ hang with it
  on, in an environment where the analytics endpoint was unreachable.
  Re-time `mm perps positions` on your own machine before deciding. The
  structural points (two calls per entry, Guard Mode approvals) still stand.

  **This is the strongest argument against running Maya's 1m scalping
  strategy through this transport.** If you want Hyperliquid, the native
  Hyperliquid API with a locally-signing agent wallet gives sub-second
  placement, atomic SL/TP and real fill history. If you want the `mm` CLI,
  raise `MAYA_TIMEFRAME` to something where tens of seconds of execution
  delay is noise (15m/1h) rather than the dominant term.
- **Size precision.** `MAYA_PERPS_SIZE_DECIMALS` defaults to 5, correct for
  BTC. Check `mm perps markets` before trading any other symbol — Hyperliquid
  rejects orders with too many size decimals.

### Settings

| Env var | Default | Meaning |
|---|---|---|
| `MAYA_VENUE` | `binance` | `binance` or `hyperliquid` |
| `MAYA_PERPS_ORDER_TYPE` | `limit` | `limit` enters at `intent.entry_price`, preserving planned R:R; `market` fills now and changes it |
| `MAYA_PERPS_SIZE_DECIMALS` | `5` | Venue size precision (BTC = 5) |
| `MAYA_PERPS_MAX_SLIPPAGE_BPS` | `50` | Slippage cap for market orders/closes |
| `MAYA_MM_TIMEOUT_S` | `120` | Per-`mm`-call process timeout; must exceed the wallet timeout |
| `MAYA_MM_WALLET_TIMEOUT_S` | `90` | `--wallet-timeout` on signing commands (CLI max 600) |
| `MAYA_MM_TELEMETRY` | `false` | `true` lets the CLI send analytics (can add long hangs) |
| `MAYA_PERPS_LIMIT_FILL_TIMEOUT_S` | `15` | How long a resting limit entry may wait for a fill before it is cancelled |
| `MAYA_PERPS_FILL_POLL_S` | `3` | Position poll interval while waiting |
| `MAYA_PULSE_ENABLED` | `false` | Pass MetaMask Pulse market summaries to the LLM veto |
| `MAYA_PULSE_TTL_S` | `900` | Pulse cache lifetime, failures included |

`MAYA_MODE=testnet` maps to Hyperliquid testnet, `live` to mainnet. Live still
requires `MAYA_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`.

## Session state across restarts

`trades_today`, `consecutive_losses`, and the day's realized PnL used to
reset to zero on every process restart — a real gap, since those back the
hard daily-trade-count and cooldown limits. They're now restored from the
trade journal (SQLite) on startup via `RiskManager.from_journal`, and the
restoration is **additively conservative**: e.g. if the journal shows a
losing streak already at the cooldown threshold, the process starts *in*
cooldown rather than assuming a clean slate. What's still not done: equity
itself isn't reconciled trade-by-trade against exchange history — for
testnet/live it's re-seeded from the current exchange USDT balance at
startup (see `_startup_reconciliation` in `maya/run.py`), which is accurate
but doesn't independently cross-check the journal's PnL math against it.

## Position monitoring (testnet/live)

The candle-driven graph only advances once per closed 1-minute candle, but
on testnet/live the actual SL/TP orders live on the exchange and can fill at
any moment. `maya/execution/monitor.py` runs a background thread (testnet/
live only) that polls the exchange every 5s, and the moment it sees the
position has gone flat there, closes it locally with the *real* fill price/
fee pulled from `fetch_my_trades` — rather than waiting up to a minute for
the next candle, or (the old bug) having the candle-driven code try to send
a second close order after the exchange already filled it. Breakeven stop
moves now also sync to the exchange (`CCXTBroker.update_stop` cancels and
replaces the live stop order) instead of only updating the local dataclass.

## Candle scheduler

The ccxt feed used to tight-poll every ~6s regardless of the actual candle
close time. `maya/data/scheduler.py` now sleeps until just after each real
timeframe boundary and backs off exponentially on fetch errors — far fewer
API calls, and candles are fetched right after they actually close instead
of being caught mid-formation.

## Real-time WebSocket feed (opt-in)

REST polling (`CCXTFeed`, the default) only sees a candle once it's already
closed, and pieces its trades together with a follow-up REST call. That's
adequate for 1m scalping but isn't the continuous tick stream the spec's CVD/
absorption indicators (§4.2) are actually designed around. `maya/data/
websocket_feed.py` subscribes directly to Binance's combined-stream endpoint
(`<symbol>@kline_<tf>` + `<symbol>@aggTrade`), buffers real trade ticks per
candle window, and emits the candle the instant Binance marks it closed.

It's **off by default** — set `MAYA_USE_WEBSOCKET=true` to opt in for paper/
testnet/live. It's kept alongside `CCXTFeed` rather than replacing it: REST
is simpler with fewer failure modes, and is what's actually been run against
testnet. Prefer REST until the WebSocket path has real testnet hours on it.
The message-parsing/candle-assembly logic (`parse_kline_event`,
`parse_agg_trade_event`, `_handle_message`) is unit-tested against crafted
Binance-format payloads — see `tests/test_websocket_feed.py` — but the live
connection itself hasn't been exercised against a real Binance endpoint from
this environment (no network path to Binance here); test it against testnet
yourself before trusting it.

## Backtesting

`maya/backtest/runner.py` runs historical candles through the **unmodified**
live pipeline (same `maya/graph.py` / `maya/nodes.py`, no separate backtest
strategy code to drift out of sync) and reports the spec §14.1 go/no-go gate
(profit factor > 1.5, max drawdown < 20%, Sharpe > 1.0 annualized, ≥200
trades):

```bash
MAYA_SYMBOL=BTCUSDT MAYA_TIMEFRAME=1m \
MAYA_BACKTEST_START=2025-01-01 MAYA_BACKTEST_END=2025-12-31 \
python -m maya.backtest.runner
```

Historical OHLCV comes from Binance's public REST klines endpoint (no keys
needed) and is cached to CSV under `data/` so repeat runs over the same
range don't re-download. **Important caveat:** getting genuine historical
tick-level trade data at 12-month scale is out of scope here, so each
candle's trades are reconstructed synthetically from its OHLCV shape (same
approach as the sim feed) — CVD/absorption signals in a backtest are an
approximation of real order flow, not the genuine article. Treat results as
directional, not a precise live forecast; that gap is exactly why the spec's
paper-trading gate (§14.2) requires live results within 30% of backtest
expectations before going further.

Drawdown and Sharpe are computed off the *realized* daily equity curve
(equity updates only when a trade closes), not a full intratrade
mark-to-market curve, so worst-case intratrade drawdown is understated
relative to a true tick-by-tick equity series.

## Is a return target realistic? (Monte Carlo simulator)

`maya/backtest/monte_carlo.py` simulates hundreds of thousands of **days** using
the risk parameters the live system actually uses, so a target return can be
checked against numbers instead of debated:

```bash
python -m maya.backtest.monte_carlo                          # just the distribution
python -m maya.backtest.monte_carlo --win-rate 0.49 --trades-per-day 5 --target 5
python -m maya.backtest.monte_carlo --compound --days 252     # annual path instead
python -m maya.backtest.monte_carlo --json                    # machine-readable
```

The reported unit is a **day**, matching the cadence the system runs at: a 1m
strategy with a 5-trades-per-day cap and a 3% daily-loss halt. Internally it
still advances a week at a time, because the 8% weekly kill switch can only
bind across days — so a simulated day sits inside a week with its own history
rather than being an isolated coin flip. Equity resets weekly (not daily) for
the same reason; resetting every day would delete that limit from the model.

`--target` is **optional**. Omit it and the report just tells you what the risk
architecture can produce; pass a daily % only to test a goal you already have.

It prints a full report — assumptions, an ASCII distribution histogram,
percentiles, how often each risk limit actually fired, and (with `--target`) a
verdict. `--ascii` forces plain output on terminals that can't render box
characters; env vars (`MC_WIN_RATE`, `MC_TRADES_PER_DAY`, `MC_STOP_DIST_PCT`,
`MC_TARGET_PCT`, `MC_SIMULATIONS`) still work as flag defaults.

> Reading the median: on a selective strategy most days have no qualifying
> setup, so a median of `+0.00%` is a **flat day**, not a breakeven trade. The
> report's "Flat days (no trade)" row separates the two.

### Position sizing, leverage and the ROI gate

The system sizes positions one of two ways, set by `MAYA_SIZING_MODE`:

| Mode | Meaning | Size is | Leverage affects |
|---|---|---|---|
| `margin` (default) | commit `MAYA_MARGIN_PER_TRADE_USD` of collateral at `MAYA_LEVERAGE` | `margin x leverage` | **position size** |
| `risk` | size so a stop-out loses `MAYA_RISK_PER_TRADE_USD` | `risk / stop distance` | margin locked only |

Defaults are **$20 margin at 20x = $400 notional**. On a 0.7% stop that risks
**$2.80** per trade, 0.28% of a $1,000 account.

> **Leverage is not risk.** In `risk` mode, position size comes from the stop
> distance, so 5x and 20x produce *identical* P&L — leverage only changes how
> much collateral is tied up and where liquidation sits. It changes your
> exposure only in `margin` mode, where it sets the position size directly.
> `tests/test_monte_carlo.py` pins this.

Margin-mode sizing is additionally clamped by `MAX_RISK_PER_TRADE_USD`, so an
unusually wide stop cannot turn a "$20 trade" into a much larger loss.

**Liquidation.** At 20x with 0.5% maintenance margin, liquidation is a **4.50%**
adverse move while the stop sits at 0.70% — the stop consumes 16% of the
buffer. `MAX_LEVERAGE` is 20; `Settings` *rejects* anything higher rather than
silently clamping it, because a config typo that doubles position size is
exactly what the hard limits exist to catch.

**`min_roi_pct` is measured on margin.** It previously compared against the raw
*price move*, so a 40% threshold demanded a 40% move in BTC — which never
happens intraday, and meant **the risk node rejected every single trade in
every mode**. It now screens `TradeIntent.roi_on_margin_pct(leverage)`: the
scenario's 4.64% move at 20x is a 92.8% return on collateral and passes. Note
this makes the gate leverage-dependent by design — the same setup that clears
40% at 20x does not at 5x.

**Cost drag is the number to watch.** Fees plus 2bps of slippage per side come
to 0.141% of notional, which against a 0.7% stop is **20.1% of every dollar you
risk**, pushing the breakeven win rate from 20% to **24.0%**. This scales
inversely with stop distance, which is why very tight stops lose to costs
regardless of win rate.

**It now models what it claimed to model.** Two corrections changed its
conclusions substantially:

1. **Sizing.** It used to simulate `RiskLimits.MAX_RISK_PER_TRADE_PCT` (0.5%
   of equity) — a constant marked "legacy" that *nothing in the live system
   reads*. The risk manager passes `Settings.risk_per_trade_usd` ($20) to
   `calculate_position_notional`, which on the $1,000 default equity is **2%
   per trade, four times more**. Fixed-USD risk also means risk-as-%-of-equity
   *rises as you lose*, which fixed-percent modeling cannot express.
2. **The risk limits are simulated.** The 3% daily halt, the 2-consecutive-loss
   cooldown, the 8% weekly kill switch, the notional cap and the equity floor
   are all enforced per simulated day. They used to be listed as known
   omissions — tolerable at 0.5% risk, where six losses fit inside the daily
   limit, but not at 2%, where **two losing trades already exceed it**. The
   cooldown alone now ends ~32% of trading days, because
   `RiskManager.on_trade_closed` resets `consecutive_losses` *only on a win* —
   so a run sitting at 2+ losses re-arms the cooldown on every further loss.

At the **current defaults** ($20 margin at 20x, notional $400, risking $2.80 a
trade) the **mean day is +0.38%** — you are risking 0.28% of the account per
trade, so the account cannot move fast. Switch to
`--risk-model fixed_usd --risk-usd 20` (risking $20 instead of committing $20)
and the same strategy gives a **+2.57% mean day**, because position size is
~7x larger. The difference is sizing, not leverage — both runs are 20x.

The **median day is +0.00% in both**, and that is not a contradiction: the
system takes ~1.3 trades a day and ~20% of days have no qualifying setup at
all, so the middle of the distribution is a flat day. The mean is carried by
the ~37% of days that lose and the tail that wins — at fixed-USD sizing the
95th-percentile day is **+15.2%** against a mean of +2.57%. Judge this system
on the mean and the tail, not the median.

> **This is not a claim that 40%/week is achievable.** The simulator models
> the *risk architecture*, and assumes whatever win rate you hand it. At 1:4
> R:R the breakeven win rate is 22.9%, so the default 45% assumption encodes
> an enormous edge — that assumption, not the arithmetic, is the thing to be
> skeptical of. Validate the win rate against the backtest and paper-trading
> gates (§14.1/§14.2) before treating any of these figures as a forecast.

By default each simulated week restarts from `--start-equity`, so results
don't depend on `--days`. Pass `--compound` for the different question — what
a year of compounding looks like — where later weeks carry much larger equity
and therefore much smaller percentage swings.

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
for paper/testnet/live. A standalone `check_connection.py` diagnostic;
journal-backed restoration of `trades_today`/`consecutive_losses`/daily PnL
on restart; a boundary-aligned candle scheduler instead of tight polling; a
background position monitor that reconciles real exchange fills faster than
the candle cadence and syncs breakeven stop moves to the exchange; an opt-in
real-time WebSocket feed (`MAYA_USE_WEBSOCKET=true`); and an event-driven
backtester that runs the unmodified live pipeline over historical klines
and reports the spec §14.1 go/no-go gate.

**Intentionally minimal (extend before live use):** funding rate / OI /
long-short ratio and the news-blackout calendar are placeholders; order-book
depth and liquidation streams from spec §4.2 aren't subscribed to yet
(kline + aggTrade only); Telegram/dashboard (L7 UI) is logging-only. State
reconciliation (spec §8.4) covers the startup gate and the ongoing position
monitor, but doesn't do a full order-book-level audit, and equity is
re-seeded from the exchange balance rather than independently cross-checked
against the journal's cumulative PnL. The backtester's trade-tick data is
synthetically reconstructed from OHLCV shape, not real historical ticks
(see `maya/backtest/data_loader.py`) — treat backtest results as directional,
not a precise live forecast. The WebSocket feed's parsing/assembly logic is
unit-tested but hasn't had real testnet connection hours on it yet.

---

## The desk (`maya/agents/`)

Five pipeline agents and four analysts. The analysts do not run in sequence —
they all read the same bar and each files a `Finding` (verdict, confidence,
evidence), which Maya combines into a `Conference`.

```python
from maya.agents import desk
conference = desk.confer(candles, signal, risk_manager, now_ts)
print(conference.summary())
# bullish(+1.21) | Atlas:bullish(0.85) Echo:bullish(0.63) Flow:bearish(0.12)
```

Why a conference instead of a bigger confluence integer: when the system
declines a trade you can read *who* disagreed and why. A score going from 7 to
6 tells you nothing.

**Maya's three rules**

1. `Sentinel`'s `BLOCK` is absolute — no weight of opinion beats "the account
   cannot take another trade".
2. A split desk stands down. If the losing side holds more than `MAX_DISSENT`
   (40%) of total opinion, Maya declines rather than sizing small. Averaging a
   genuine conflict is how a system ends up trading its own confusion.
3. Weights (`Atlas` 1.0, `Echo` 0.7, `Flow` 0.6) are **declared, not fitted**.
   Fitting them to the same window that produced the measurements would be
   curve-fitting with extra steps.

### Atlas — support and resistance as *zones*

A level is a region price repeatedly cared about, not a line through one wick.
Atlas clusters confirmed swings (which need bars on both sides, so no lookahead)
into zones scored by touches (50%), volume (30%) and recency (20%). Zones pad to
a minimum width — touches landing on the identical print would otherwise give a
zero-width zone price can never be *inside*.

### Echo — patterns in context

The pattern library (`maya/signals/patterns.py`) is context-free by design: a
hammer is a shape. Echo supplies the meaning. Shape alone earns 0.25; a pattern
printing at a zone that agrees with it earns up to +0.45 more, scaled by zone
strength. Context is judged on the bar's **range**, not its close — a rejection
candle tests a level with its wick and closes away from it.

### Flow — and a deliberate inversion

`DELTA_IS_MOMENTUM = False`. Measured over 24 months, positive delta preceded
negative forward returns in 8/8 sub-periods on BTC and SOL. Flow encodes the
measured reading; the legacy setups in `maya/strategy/setups.py` are **not**
flipped, because silently inverting a live strategy on a sub-2bps effect would
be reckless. Change one, deliberately, when you decide to.

## Technicals & patterns (`maya/signals/`)

`oscillators.py` — SMA, EMA, ATR, RSI, MACD, Bollinger, VWAP, SuperTrend.
Wilder smoothing where the definition calls for it, so values match charting
packages. Every function returns `None` rather than a short-window value: a
14-period RSI seeded from 4 bars is a different indicator, not a weaker one.

`patterns.py` — doji, marubozu, hammer, shooting star, bullish/bearish
engulfing, morning star, evening star, inside bar. All thresholds are ratios,
so a $0.30 SOL candle and a $65,000 BTC candle read identically.

`technicals.py` — EMA trend with a dead band (without it, EMAs sitting on top
of each other flip the trend every bar), swing structure, S/R levels.

`timeframes.py` — folds the 1m stream into higher timeframes. Buckets are
epoch-aligned, and a bar is published only once a later bar proves it closed,
so backtest and live see the same bar at the same moment.
