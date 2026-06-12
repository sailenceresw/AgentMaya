# CLAUDE.md

Guidance for AI assistants (Claude Code and others) working in this repository.

## What this is

**AgentMaya** ("Maya Trading System") is an autonomous order-flow scalping
agent for **Binance USD-M Perpetual Futures**, trading 1-minute candles with a
Fabervaale-style methodology (CVD, Volume Profile, absorption/sweep patterns)
behind strict, hard-coded risk management.

The repository is a **runnable MVP** built as a **LangGraph** application. It
runs end-to-end in safe **simulation mode by default** — no API keys, no real
orders. Live trading exists but is gated behind an explicit interlock.

> ⚠️ This trades (or can trade) **real money**. The risk layer and the live
> interlock are the most safety-critical code in the repo. Treat the hard
> limits in `maya/config.py` and the guards in `maya/run.py` as load-bearing
> safety mechanisms — do not loosen, bypass, or "optimize them away" without an
> explicit request, and even then, flag the safety implications.

## Repository layout

```
docs/
  MAYA_TRADING_SYSTEM_SPEC.md   # The design blueprint (7 layers). Source of truth for intent.
  RUNNING.md                    # How to run; implemented vs. stubbed.
maya/                           # The application package
  config.py                     # Settings (env) + RiskLimits (immutable constants)
  state.py                      # TradingState TypedDict passed between graph nodes
  graph.py                      # LangGraph StateGraph wiring (one invoke per candle)
  nodes.py                      # The 5 graph nodes; holds the stateful layer services
  run.py                        # CLI entrypoint / ingestion loop / live guard / reporting
  __main__.py                   # `python -m maya` -> run.main()
  data/        # L1 ingestion: models (Candle/Trade), feeds (sim/ccxt), scripted scenarios
  signals/     # L2: indicator functions + SignalProcessor + SignalObject model
  strategy/    # L3: setups, confluence scoring, optional LLM veto, StrategyEngine
  risk/        # L4: RiskManager (hard limits, kill switch), position sizing
  execution/   # L5: ExecutionEngine, brokers (paper/ccxt), Position model
  journal/     # L6: SQLite trade journal + performance metrics
tests/         # indicators, risk limits, end-to-end integration through the graph
.env.example   # Copy to .env; documents every setting and the live interlock
requirements.txt
```

## Architecture: the 7 layers and the graph

The spec defines seven layers. The runnable implementation maps them onto a
single-pass LangGraph `StateGraph` that is **invoked once per candle**:

```
START → manage (L5) → signal (L2) → strategy (L3) → risk (L4) → execution (L5) → END
```

| Layer | Where | Role |
|---|---|---|
| L1 Ingestion | `data/ingestion.py`, `data/scenarios.py` | Yields `Candle`s (with underlying `Trade`s) from a feed. Lives in the runner loop, not a node. |
| L2 Signals | `signals/` (`signal` node) | Computes CVD, volume profile, absorption, divergence, AMT phase → `SignalObject`. |
| L3 Strategy | `strategy/` (`strategy` node) | Runs 3 setups, scores confluence, optional LLM veto → at most one `TradeIntent`. **Never places orders.** |
| L4 Risk | `risk/` (`risk` node) | Kill switch + hard-limit screening + position sizing → `RiskDecision`. **Has veto power over everything.** |
| L5 Execution | `execution/` (`manage` + `execution` nodes) | `manage` advances open positions (SL/TP/breakeven/duration); `execution` opens approved trades. |
| L6 Journal | `journal/store.py` | SQLite record of every closed trade + metrics. Written from the `manage` node. |
| L7 Monitoring | logging (`loguru`) + journal metrics | No separate UI; structured logs + the end-of-run report in `run.py`. |

### Key data flow conventions

- **One `TradingState` (TypedDict) per candle** flows through the nodes
  (`maya/state.py`). It carries only per-candle artifacts
  (`candle`, `signal`, `intent`, `risk_decision`, `closed_trade`, `opened`,
  `halted`). Heavy, stateful service objects (`SignalProcessor`,
  `StrategyEngine`, `RiskManager`, `ExecutionEngine`, `TradeJournal`) live in
  `TradingNodes` (`maya/nodes.py`) and are captured by the node closures — they
  are **not** stored in graph state.
- **Single-position system.** `MAX_CONCURRENT_POSITIONS = 1`. The strategy node
  short-circuits and emits no intent while a position is open.
- **`manage` runs first** each candle so an open position is resolved against
  the new bar before any new entry is considered.
- Each layer produces a typed object consumed by the next:
  `Candle → SignalObject → TradeIntent → RiskDecision → Position/TradeResult`.

## Running, building, testing

This is a plain Python package — **no build step**, no packaging config beyond
`requirements.txt`. Python 3.10+ (uses `X | Y` unions and `from __future__
import annotations` throughout).

```bash
python -m pip install -r requirements.txt

# Default: full pipeline on synthetic data, no keys, no orders.
python -m maya

# Demo: replay a scripted textbook liquidity-sweep through the REAL strategy/risk/execution
# so you can watch one trade flow signal→strategy→risk→execution→journal end-to-end.
MAYA_SCENARIO=true MAYA_MAX_CANDLES=0 python -m maya

# Tests (indicators, risk limits, integration through the graph)
python -m pytest -q
```

`ccxt` and `anthropic` are **imported lazily** (only when a real-exchange or LLM
mode is actually used), so simulation runs even if those packages are absent or
misconfigured. Keep that laziness — do not add top-level imports of `ccxt`/
`anthropic` into modules that run in `sim` mode.

### Run modes (`MAYA_MODE`)

| Mode | Data | Orders | Keys |
|---|---|---|---|
| `sim` (default) | synthetic | simulated (paper) | none |
| `paper` | real Binance | simulated (paper) | Binance (read) |
| `testnet` | Binance testnet | real sandbox orders | Binance testnet |
| `live` | real Binance | **REAL MONEY** | Binance + interlock |

`live` mode **refuses to start** unless `MAYA_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`
**and** API credentials are present (`_guard_live` in `maya/run.py`, exits 2
otherwise). This guard is intentional — preserve it.

## Configuration & the hard limits

Two distinct config concepts in `maya/config.py` — keep them separate:

- **`Settings`** (`pydantic-settings`): user-facing, loaded from env / `.env`,
  prefixed `MAYA_` (e.g. `MAYA_MODE`, `MAYA_SYMBOL`). Credentials
  (`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `ANTHROPIC_API_KEY`) are read
  **without** the prefix. Accessed via the cached `get_settings()`.
- **`RiskLimits`**: hard-coded safety constants mirroring spec §7.1
  (`MAX_RISK_PER_TRADE_PCT`, `MAX_TRADES_PER_DAY`, `DAILY_LOSS_LIMIT_PCT`,
  `MIN_RR_FOR_ENTRY = 4.0`, `MIN_CONFLUENCE_SCORE = 7`, etc.). **Intentionally
  NOT environment-overridable** — they are compiled-in constants. Do not add env
  overrides for these unless explicitly asked, and if asked, surface the risk.

## Conventions to follow when editing

- **Module = one spec layer.** Each file's docstring cites the relevant spec
  section (e.g. "spec section 7.1"). When you change behavior, keep the
  docstring and the spec reference accurate; if intent diverges from
  `docs/MAYA_TRADING_SYSTEM_SPEC.md`, call it out.
- **Style:** `from __future__ import annotations` at the top of every module;
  type hints everywhere; `dataclass` for data carriers; `TypedDict` for graph
  state; `Protocol` for the pluggable `CandleFeed` / `Broker` seams.
- **Logging is `loguru`**, brace-style: `logger.info("x={}", x)`. Don't use the
  stdlib `logging` module or f-strings inside log calls. The `[PAPER]`/`[TESTNET]`
  prefixes in broker logs are a deliberate convention.
- **Setups are pure functions** of `(SignalObject, SetupContext) -> TradeIntent | None`,
  registered in `ALL_SETUPS` (`strategy/setups.py`). Add new setups there.
- **Indicators are pure functions** over candle/series lists (`signals/indicators.py`),
  orchestrated by the stateful `SignalProcessor`. Keep indicator math
  side-effect-free and easily unit-testable.
- **The LLM layer can only VETO, never originate**, and **fails open (approves)**
  on any error/disabled state (`strategy/llm_filter.py`). The deterministic risk
  layer is the hard gate; the LLM is a second opinion only. Preserve this
  asymmetry. Default LLM model is `claude-sonnet-4-6` (`MAYA_LLM_MODEL`).
- **Order-flow sign convention:** `Trade.is_buyer_maker == True` means an
  **aggressive SELL** (maker is the buyer); `False` means an aggressive BUY.
  CVD = aggressive buys − aggressive sells. Get this backwards and every signal
  inverts.
- **Conservative fill assumptions:** in `manage`, the stop is checked **before**
  the target on the same bar; breakeven moves apply from the next bar onward.
  Fees are taker-rate (`FEE_RATE = 0.0005`) on both entry and exit. Keep
  simulation pessimistic, not optimistic.
- **New runtime artifacts** (DBs, logs) must stay git-ignored. `*.db`,
  `maya_journal.db`, `*.log`, `.env` are already in `.gitignore`. Never commit a
  real `.env` or any secret.

## Testing notes

- `tests/test_indicators.py` — order-flow math (pure functions).
- `tests/test_risk.py` — the safety-critical risk layer: sizing caps, R:R
  rejection, halt-on-loss-limit, single-position enforcement, cooldown.
- `tests/test_integration.py` — drives the **scripted scenario** through the
  full graph and asserts exactly one winning trade is journaled using the
  *unmodified* strategy/risk/execution logic. This is the end-to-end guard.

When you change strategy, risk, or execution behavior, run the integration test
— the scenario in `data/scenarios.py` is hand-tuned to satisfy the real
confluence (≥7) and R:R (≥4–5) gates, so changes to those gates can break it
legitimately. If they do, understand *why* before adjusting the scenario.

## What is implemented vs. intentionally minimal

**Implemented & runnable:** all 7 layers wired through LangGraph; the 3 setups;
confluence scoring; the full hard-limit risk gate + kill switch + cooldown;
position sizing; paper execution with SL/TP/breakeven/duration management; the
SQLite journal + metrics; the optional LLM veto; a ccxt broker/feed for
paper/testnet/live.

**Intentionally minimal (extend before live use):** the ccxt feed *polls*
klines rather than using websocket streams (spec §4.2); funding rate / OI /
long-short ratio and the news-blackout calendar are placeholders defaulting to
neutral; there is no event-driven backtester; L7 UI is logging-only;
state reconciliation on restart (spec §8.4) is not implemented.

When asked to extend the system, prefer aligning with the spec's design for that
area over inventing a new one, and keep `docs/RUNNING.md`'s "implemented vs.
stubbed" section honest.

## Git workflow

- Default/main branch: `main`. Develop on a feature branch and push with
  `git push -u origin <branch>`; retry transient network failures with
  exponential backoff.
- Commit only when asked; never push to a branch other than the one you were
  asked to use without explicit permission.
- Do **not** open a pull request unless explicitly requested.

## Safety reminders for assistants

1. Never weaken the live-trading interlock or the `RiskLimits` without an
   explicit request, and surface the consequences when you do.
2. Keep simulation the default; never make a real-order mode the path of least
   resistance.
3. Don't commit secrets or runtime DBs/logs.
4. Preserve the LLM veto-only / fail-open contract and the order-flow sign
   convention — both are subtle and easy to invert.
</content>
</invoke>
