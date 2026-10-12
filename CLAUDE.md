# CLAUDE.md

Guidance for AI coding assistants working in this repository. Human-facing
docs: `README.md` (overview), `GUIDE.md` (orientation), `docs/RUNNING.md`
(how to run everything), `docs/MAYA_TRADING_SYSTEM_SPEC.md` (design intent).

## What this is

**AgentMaya** is an autonomous crypto-perpetuals trading agent built as a
**LangGraph** pipeline (one graph invocation per closed candle). It trades
either **Binance USD-M** (ccxt, API keys) or **Hyperliquid** through the
**MetaMask Agent Wallet `mm` CLI** (self-custodial, no exchange keys). It runs
in safe **simulation mode by default**: no keys, no orders.

> ⚠️ It can trade **real money**. The risk layer, the fee gate, the liquidation
> guard and the live interlock are load-bearing safety code. Never loosen,
> bypass or "optimise away" a limit without an explicit request, and say what
> the change risks when you do.

> 📉 **Measured performance:** a 24-month real-order-flow study (README,
> "Measured performance") found **no predictive signal** — best backtest
> profit factor 0.38. Do not present the strategy as profitable, and do not
> tune thresholds to make a backtest look better. New ideas must be measured
> with `maya.backtest.sweep` (fees included) on data they were not chosen on.

## Commands

```bash
python -m pip install -r requirements.txt
python -m pytest -q                                   # all tests, ~10s, no network
ruff check maya tests --select F,E9,B                 # keep this clean
python -m maya                                        # sim, 200 candles
MAYA_SCENARIO=true MAYA_MAX_CANDLES=0 python -m maya  # scripted demo trade, end to end
python -m maya.scanner                                # multi-coin scan (needs MAYA_MODE=paper)
python -m maya.backtest.sweep --symbols ... --timeframes 15m,1h   # fee-aware measurement
python -m maya.data.download ...                      # fetch/import backtest data once
```

Tests never touch the network: exchanges, the `mm` CLI and the Anthropic
client are replaced with fakes. Keep it that way.

## Layout

```
maya/
  config.py        Settings (MAYA_* env) + RiskLimits (immutable hard limits)
  graph.py nodes.py   manage → signal → strategy → risk → execution, per candle
  run.py           single-symbol agent: preflight, feed, watchdog, API, shutdown
  scanner.py scan_run.py   multi-coin scanner and the agent that trades its pick
  fees.py          per-venue fee model (maker/taker legs + MetaMask builder fee)
  data/            feeds (sim/scenario/ccxt REST/websocket), scheduler, watchdog, download
  signals/         CVD, volume profile, absorption, AMT, ATR, patterns, HTF folding
  strategy/        3 setups, confluence, optional LLM veto (Claude, structured verdict)
  agents/          the desk (Atlas/Echo/Flow/Sentinel) — analysis/veto only, never a signal
  risk/            RiskManager: hard limits, kill switch, sizing, liquidation + fee gates
  execution/       ExecutionEngine; Paper, ccxt (Binance) and MetaMask/Hyperliquid brokers;
                   PositionMonitor (venue fills between candles)
  journal/         SQLite journal; restores risk state across restarts
  backtest/        runner (same graph as live), sweep, metrics, plot, Monte Carlo, funding
  api/server.py    token-protected monitoring API + phone dashboard + halt switch
  check_connection.py / check_metamask.py   startup preflights (Binance / Hyperliquid)
android/           Android phone monitor for the API (Java, no dependencies)
ios/               iOS phone monitor (SwiftUI app package, iOS 17)
tests/             one test module per area; regression tests name the bug they pin
                   (test_app_contract.py pins the API fields both phone apps read)
```

## Rules that keep this system honest

- **Backtest ≡ live.** The backtester runs the unmodified graph. Never add
  backtest-only strategy logic, and never let a signal read a candle before
  it closed (HTF bars publish only when the next bucket starts; swings need
  confirmed neighbours; the compression range excludes the current candle).
- **Fees are real.** PnL is charged leg by leg from `maya/fees.py`; through
  MetaMask a round trip is up to 0.26%. The risk layer rejects trades whose
  net R:R or fee share fails. Never revert to a single flat fee number.
- **The risk layer is the hard gate.** The LLM veto and the desk may only
  remove trades. Every LLM failure path *approves* (fails open) precisely
  because risk is enforced deterministically elsewhere.
- **Stops live at the venue.** On Hyperliquid, SL/TP attach by a second
  `mm perps modify` after a confirmed fill; a resting entry is waited on and
  cancelled, never left naked. On Binance, `closePosition` stops must not
  carry `reduceOnly` or a quantity (Binance rejects them).
- **Unverified ≠ verified.** `docs/RUNNING.md` separates what was checked
  against a live venue from what was read from source or assumed. Keep that
  distinction in docs, docstrings and test fixtures (label invented fixtures).
- **Halts are sticky** except daily/weekly loss halts, which a new period
  lifts. Stale-data, equity-floor and manual halts need a human.
- **Defaults are safe.** Sim mode, no live trading without
  `MAYA_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK`, unmeasured setups off
  (`MAYA_COMPRESSION_BREAKOUT_ENABLED=false`), API off and token-required.

## Conventions

- Python 3.11+, `from __future__ import annotations`, type hints, loguru logging.
- Comments explain *why* (often the bug a line prevents), not what.
- Every bug fix gets a regression test that fails on the old code.
- Config lives in `Settings`; document new settings in `.env.example` and the
  settings table in `docs/RUNNING.md`. Blank optional numbers mean "default".
- Commits are authored by the repository owner; do not add AI co-author
  trailers unless asked.
