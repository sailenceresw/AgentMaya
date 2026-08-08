# Maya — Guide to This Codebase

This is a working snapshot of everything built across our sessions. Read this
before changing anything — it tells you which files are safe to experiment
with and which ones exist specifically so you *don't* need to touch them.

## 1. How to get oriented

```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
pytest -q                                                   # should show ~56 passed
python -m maya                                              # sim mode, no keys needed
MAYA_SCENARIO=true MAYA_MAX_CANDLES=0 python -m maya         # scripted demo trade
```

If those three all work, the codebase on your machine matches what's been
verified. Start every session by re-running `pytest -q` after you change
anything — it's fast (a few seconds) and it's the cheapest way to know
immediately if you broke something.

## 2. Repo map — what lives where

```
maya/
  config.py          Settings (env-driven) + RiskLimits (hard-coded constants)
  run.py              the main entrypoint: python -m maya
  check_connection.py standalone Binance connectivity/account diagnostic
  graph.py, nodes.py   LangGraph wiring — the pipeline shape
  state.py             the shared state object passed between graph nodes

  data/
    ingestion.py        feed selection (sim / scenario / ccxt / websocket)
    websocket_feed.py    opt-in real-time feed (MAYA_USE_WEBSOCKET=true)
    scheduler.py         boundary-aligned polling for the REST feed
    models.py             Candle / Trade data classes
    scenarios.py          scripted demo setups

  signals/            order-flow indicators (CVD, volume profile, absorption)
  strategy/           the 3 ICT/SMC setups + confluence scoring + direction (long/short)
  risk/
    manager.py          AccountState + all the hard limits + cooldown/halt logic
    sizing.py            position sizing formula

  execution/
    engine.py            position lifecycle: open/manage/close, SL/TP/breakeven
    brokers.py            PaperBroker (sim) vs CCXTBroker (testnet/live)
    monitor.py             background thread reconciling real exchange fills

  journal/store.py     SQLite trade log + the counters risk state restores from

  backtest/
    runner.py             replays historical candles through the live pipeline
    data_loader.py         fetches/caches historical klines
    metrics.py              profit factor / drawdown / Sharpe / gate check
    monte_carlo.py          weekly-return distribution simulator

docs/
  MAYA_TRADING_SYSTEM_SPEC.md   the original full spec — the source of truth
  RUNNING.md                     day-to-day operator instructions, updated as we build

tests/                 one file per module above, ~56 tests total
```

## 3. Where you should NOT casually make changes

These exist specifically to survive a bad prompt, a bad day, or a bad idea —
including your own, three months from now, at 2am, after two losses in a row.
Changing them isn't forbidden, but it should be a deliberate, separate
decision, not a side effect of some other change.

- **`maya/config.py` → `RiskLimits` class.** `MAX_RISK_PER_TRADE_PCT` (0.5%),
  `MAX_LEVERAGE` (10x), `MIN_RR_FOR_ENTRY` (4.0), `MAX_TRADES_PER_DAY` (5),
  `MAX_CONSECUTIVE_LOSSES`/cooldown, daily/weekly loss limits, equity floor.
  No LLM code path can reach these — keep it that way.
- **`maya/risk/manager.py`.** The veto logic. If you add an LLM-assisted
  layer here later, it must stay *additive* (can only make a trade more
  restrictive, never approve something the hard limits already rejected).
- **`maya/execution/engine.py` locking (`self.lock`)** — this is what
  prevents the candle-driven loop and the background position monitor from
  racing on the same position. Don't remove it to "simplify" something.
- **The double gate for live trading** in `run.py` (`_guard_live`) —
  `MAYA_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK` plus real credentials. Don't
  shortcut this even for testing; test the shortcut path in `sim` mode instead.

## 4. Where it's genuinely fine to experiment

- **`maya/strategy/`** — the actual setup definitions and confluence scoring.
  This is where your trading judgment belongs, and where you should expect to
  spend the most iteration time.
- **`maya/signals/`** — indicator calculations. Add new ones here.
- **`data/scenarios.py`** — add more scripted setups to sanity-check strategy
  changes without needing live data.
- **`backtest/monte_carlo.py` assumptions** — override via env vars
  (`MC_WIN_RATE`, `MC_TRADES_PER_WEEK`, etc.) to stress-test ideas before
  touching real code.

## 5. Concrete next steps, in order

1. **Set up `.env`** from `.env.example`. Generate a *fresh* testnet key pair
   at testnet.binancefuture.com (regenerate — don't reuse any key that was
   ever pasted into a chat).
2. **Run `python -m maya.check_connection`.** Fix anything it flags before
   moving on. It'll tell you your real testnet balance — that number, not a
   guess, is what your risk-per-trade will actually be at 0.5%.
3. **Run `MAYA_MODE=testnet python -m maya`** for a real (but fake-money)
   session. Watch the logs. This is the first time the strategy touches real
   market data — expect it to behave differently than the scripted demo.
4. **Let it accumulate real testnet trades**, then check `journal.metrics()`
   / the SQLite file directly — does the realized win rate and R:R look
   anything like the 43-49% / 1:4 assumption the whole risk model is built
   around? This is the single most important question before scaling
   anything.
5. **Only after that** — revisit the app (Telegram bot / dashboard), and
   only after that, ever, consider live mode.

## 6. What's still genuinely unfinished (see RUNNING.md for full detail)

- WebSocket feed has zero real connection hours — logic is unit-tested, not
  network-tested.
- Backtester's trade-tick data is synthetic (reconstructed from OHLCV
  shape), not real historical ticks — treat backtest results as directional.
- No funding-rate/OI/news-blackout signals yet — placeholders only.
- No app/dashboard yet — CLI + logs only, by design, until the core loop has
  real hours on it.

## 7. If you get confused mid-session

Come back to this file, or to `docs/RUNNING.md` for the operator-level
detail. The spec (`docs/MAYA_TRADING_SYSTEM_SPEC.md`) is the original design
intent if you want to check whether something is a deliberate choice or a
gap. And `pytest -q` is always the fastest way to check "did I just break
something" before you keep going.
