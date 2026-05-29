# MAYA TRADING SYSTEM
## Autonomous AI Trading Agent — Architecture & Technical Specification

**Binance Perpetual Futures · 1-Minute Scalping · Order Flow Strategy**

- **Document Version:** 1.0
- **Prepared by:** Maya AI Trading System
- **Date:** May 17, 2026
- **Classification:** CONFIDENTIAL — TECHNICAL SPECIFICATION

---

## 1. Executive Summary

This document specifies the architecture for an autonomous AI trading agent that executes order flow scalping strategies on Binance USD-M Perpetual Futures. The system implements a Fabervaale-style methodology adapted for cryptocurrency markets, combining order flow analysis, volume profile theory, and machine learning components.

### 1.1 Project Goals

- Build a 24/7 autonomous trading system for BTCUSDT perpetual futures
- Implement order flow-based decision logic (CVD, Volume Profile, Absorption patterns)
- Maintain strict risk management with hard-coded safety limits
- Provide complete observability via logging, alerts, and dashboards
- Enable continuous learning from trade outcomes

### 1.2 Non-Goals

- This is **NOT** a high-frequency trading system (operates on 1m candles, not microseconds)
- This is **NOT** a market-making bot (directional strategy only)
- This is **NOT** designed for hundreds of pairs (BTC-focused, expandable to ETH/SOL)
- This is **NOT** a replacement for the trader's judgment — operates within defined rules

> **CRITICAL DISCLOSURE:** Algorithmic trading involves substantial risk of capital loss. This specification provides architecture only and does not guarantee profitability. The trader's manual strategy must already be proven profitable before automation. Automating an unprofitable strategy multiplies losses.

---

## 2. System Architecture Overview

### 2.1 High-Level Architecture

The system consists of seven primary layers, each with distinct responsibilities and isolation boundaries:

```
┌─────────────────────────────────────────────────────────┐
│   LAYER 7: USER INTERFACE & MONITORING                  │
│   Telegram Bot · Web Dashboard · Email Alerts           │
├─────────────────────────────────────────────────────────┤
│   LAYER 6: LEARNING & ANALYTICS                         │
│   Trade Journal DB · Performance Metrics · ML Retrain   │
├─────────────────────────────────────────────────────────┤
│   LAYER 5: EXECUTION ENGINE                             │
│   Order Manager · Position Tracker · SL/TP Manager      │
├─────────────────────────────────────────────────────────┤
│   LAYER 4: RISK MANAGEMENT (FAIL-SAFE)                  │
│   Position Sizer · Drawdown Monitor · Kill Switch       │
├─────────────────────────────────────────────────────────┤
│   LAYER 3: STRATEGY ENGINE                              │
│   Rule-Based Logic · AI Confluence Scorer · Setup Match │
├─────────────────────────────────────────────────────────┤
│   LAYER 2: SIGNAL PROCESSING                            │
│   CVD · Volume Profile · Pattern Detection · Indicators │
├─────────────────────────────────────────────────────────┤
│   LAYER 1: DATA INGESTION                               │
│   Binance WS/REST · Coinglass API · Local Cache         │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Core Design Principles

- **Fail-safe over fail-soft:** When in doubt, do nothing. No trade is better than a bad trade.
- **Idempotent operations:** Every order placement must be safe to retry.
- **Separation of concerns:** Strategy logic NEVER touches risk management overrides.
- **Stateful recovery:** System must resume cleanly after crash/restart.
- **Observable by design:** Every decision must be loggable and explainable.
- **Defense in depth:** Multiple independent safety layers.

---

## 3. Technology Stack

### 3.1 Required Technologies

| Component | Recommended Choice |
|---|---|
| Language | Python 3.11+ |
| Exchange SDK | python-binance OR ccxt |
| Async Framework | asyncio + aiohttp |
| Database | PostgreSQL 15+ (production) or SQLite (dev) |
| Time-series DB | TimescaleDB (PostgreSQL extension) |
| Cache | Redis 7+ |
| Task Queue | Celery or APScheduler |
| AI Layer (optional) | CrewAI OR LangChain OR direct OpenAI/Anthropic SDK |
| Backtesting | Backtrader OR Vectorbt OR custom |
| Monitoring | Prometheus + Grafana |
| Logging | Loguru + structured JSON logs |
| Alerts | python-telegram-bot |
| Deployment | Docker + docker-compose |
| VPS/Cloud | AWS EC2, DigitalOcean, or Hetzner (low-latency to Binance) |

### 3.2 Critical Library Recommendations

#### 3.2.1 Binance Integration

Use `python-binance` (most mature) OR `ccxt` (multi-exchange abstraction). For pure Binance focus, `python-binance` is recommended for its WebSocket implementation and futures-specific endpoints.

```bash
pip install python-binance==1.0.19
# OR for multi-exchange flexibility:
pip install ccxt==4.2.0
```

#### 3.2.2 Technical Analysis

```bash
pip install pandas-ta==0.3.14b0   # Technical indicators
pip install numpy pandas           # Data manipulation
pip install scipy                  # Statistical functions
```

#### 3.2.3 Backtesting

Recommended: Vectorbt for vectorized speed, or Backtrader for accuracy. For Fabervaale-style order flow strategies, build a custom event-driven backtester since standard frameworks don't natively support order flow data.

### 3.3 Infrastructure Requirements

| Resource | Minimum Specification |
|---|---|
| CPU | 2 vCPU (4 vCPU recommended) |
| RAM | 4 GB minimum, 8 GB recommended |
| Storage | 50 GB SSD (database grows) |
| Bandwidth | Unmetered or 1 TB/month |
| Region | AWS Tokyo OR Singapore (low latency to Binance servers) |
| Uptime SLA | 99.9% minimum |
| Estimated Cost | $20-40/month |

> **LATENCY MATTERS:** Binance servers are in Tokyo. A VPS in the US adds 100-150ms round trip. For 1m scalping this is acceptable, but for tighter timeframes, choose Asia-Pacific hosting.

---

## 4. Layer 1 — Data Ingestion

### 4.1 Responsibilities

- Maintain real-time WebSocket connections to Binance Futures
- Pull historical data for indicator initialization
- Aggregate trade tick data into custom timeframes (1m, 5m, 15m)
- Fetch ancillary data: funding rates, open interest, liquidations
- Handle reconnections, gaps, and out-of-sequence data
- Provide unified data interface to upper layers

### 4.2 Binance WebSocket Streams Required

| Stream | Purpose | Update Frequency |
|---|---|---|
| aggTrade@btcusdt | Aggregated trade data for CVD calculation | Real-time |
| btcusdt@kline_1m | 1-minute candle close events | Every second |
| btcusdt@kline_5m | 5-minute candle close events | Every second |
| btcusdt@kline_15m | 15-minute candle close events | Every second |
| btcusdt@depth20@100ms | Order book top 20 levels | 100ms |
| btcusdt@markPrice | Mark price + funding rate | 1 second |
| btcusdt@forceOrder | Liquidation events | Real-time |
| btcusdt@bookTicker | Best bid/ask | Real-time |

### 4.3 REST API Endpoints Required

| Endpoint | Purpose | Rate Limit |
|---|---|---|
| GET /fapi/v1/klines | Historical candles for backfill | 1200/min |
| GET /fapi/v1/openInterest | Current OI snapshot | 1000/min |
| GET /fapi/v1/fundingRate | Historical funding rates | 1000/min |
| GET /fapi/v2/account | Account balance + positions | 10/sec |
| POST /fapi/v1/order | Place orders | 300/10sec |
| DELETE /fapi/v1/order | Cancel orders | 300/10sec |
| GET /fapi/v1/exchangeInfo | Symbol metadata | Periodic |

### 4.4 External Data Sources

| Source | Data Provided |
|---|---|
| Coinglass API | Liquidation heatmaps, long/short ratios, OI history |
| CryptoQuant (optional) | On-chain flows, exchange reserves |
| Alternative.me | Fear & Greed Index for macro context |
| Economic Calendar API | Macro events (CPI, FOMC) for trading halts |

### 4.5 Implementation Pattern

```python
class BinanceDataIngestion:
    async def start(self):
        self.ws_client = AsyncClient(api_key, api_secret)
        self.bsm = BinanceSocketManager(self.ws_client)
        # Subscribe to required streams
        await self.subscribe_klines('BTCUSDT', ['1m', '5m', '15m'])
        await self.subscribe_agg_trades('BTCUSDT')
        await self.subscribe_depth('BTCUSDT')
        # Start consumer tasks
        asyncio.create_task(self._handle_messages())
        asyncio.create_task(self._reconnect_monitor())
```

### 4.6 Critical Requirements

> Data integrity is paramount. Missing ticks, out-of-order trades, or stale prices can cause catastrophic trading errors. Implement: timestamp validation, gap detection, automatic reconnection with backfill, and circuit breakers if data quality degrades.

---

## 5. Layer 2 — Signal Processing

### 5.1 Responsibilities

Transform raw market data into actionable signals matching Fabervaale-style methodology. This layer is computationally intensive and must complete within 200ms per candle.

### 5.2 Required Indicators & Calculations

#### 5.2.1 Cumulative Volume Delta (CVD)

Calculate per candle: sum of buy_volume minus sell_volume from aggregated trade data. Maintain rolling CVD over multiple timeframes.

```python
def calculate_delta(trades):
    buy_vol = sum(t.qty for t in trades if not t.is_buyer_maker)
    sell_vol = sum(t.qty for t in trades if t.is_buyer_maker)
    return buy_vol - sell_vol
```

#### 5.2.2 Volume Profile (VAH / POC / VAL)

Calculate volume distribution across price levels over rolling windows (session, day, week). Identify Point of Control (POC), Value Area High (VAH), Value Area Low (VAL) using the 70% volume rule.

#### 5.2.3 Absorption Detection

Algorithmic detection of absorption patterns: multiple wicks at same price level with high volume but no directional progress.

- Identify candles with >X% wick relative to body
- Cluster wicks within Y price ticks of each other
- Confirm with volume profile concentration
- Flag as absorption signal when 3+ wicks cluster

#### 5.2.4 CVD Divergence Detection

Compare price swing points vs CVD swing points over rolling window. Classify as:

- **Bullish divergence:** price LL, CVD HL
- **Bearish divergence:** price HH, CVD LH
- **Hidden bullish:** price HL, CVD LL
- **Hidden bearish:** price LH, CVD HH

#### 5.2.5 Auction Market Theory Phase Detection

Classify current market regime using statistical features:

- **Accumulation:** Tight range, balanced volume, flat CVD
- **Expansion:** Wide candles, trending CVD, volume spike
- **Distribution:** Tight range at extremes, conflicting signals
- **Contraction:** Choppy, low volume, mean-reverting

### 5.3 Output Format

Layer 2 outputs a unified signal object on each candle close:

```python
{
  'timestamp': 1715958060000,
  'symbol': 'BTCUSDT',
  'price': 67500.50,
  'cvd_1m': 1250.5,
  'cvd_session': 8500.2,
  'cvd_divergence': 'bullish',
  'volume_profile': {
     'vah': 67800, 'poc': 67450, 'val': 67200,
     'shape': 'D'  # D / P / b / trend
  },
  'absorption': {
     'detected': True, 'side': 'bullish',
     'level': 67220, 'strength': 0.85
  },
  'amt_phase': 'expansion',
  'funding_rate': 0.0085,
  'long_short_ratio': 1.85
}
```

---

## 6. Layer 3 — Strategy Engine

### 6.1 Responsibilities

Consume signal objects from Layer 2 and produce trade decisions. This is where Fabervaale's methodology is codified into executable rules. The strategy engine NEVER places orders directly — it produces signed intents that pass through Risk Management (Layer 4).

### 6.2 The Three Production Setups

#### Setup 1: Value Area Absorption

| Parameter | Specification |
|---|---|
| Trigger | Price at VAL or VAH ± 0.1% |
| Confirmation 1 | 3+ absorption wicks at level within 5 candles |
| Confirmation 2 | CVD divergence in setup direction |
| Confirmation 3 | Funding rate aligned (e.g., positive funding for shorts at VAH) |
| Direction | Long at VAL with bullish absorption / Short at VAH with bearish |
| Stop Loss | Beyond absorption wall, max 0.3% from entry |
| Take Profit 1 | POC or 50% to opposing value area |
| Take Profit 2 | Opposing value area boundary |
| Min R:R | 1:4 |

#### Setup 2: Liquidity Sweep + ChoCH

| Parameter | Specification |
|---|---|
| Trigger | Wick beyond prior 15m swing high/low + close back inside |
| Confirmation 1 | Volume spike on sweep (>2x avg) |
| Confirmation 2 | CVD shows opposite-direction aggression absorbed |
| Confirmation 3 | 1m structure breaks opposite to sweep |
| Direction | Counter to swept direction |
| Stop Loss | Beyond swept wick, max 0.4% from entry |
| Take Profit | Next 15m liquidity pool |
| Min R:R | 1:5 |

#### Setup 3: Compression Breakout

| Parameter | Specification |
|---|---|
| Trigger | Range < 0.3% for 30+ candles, CVD flat |
| Confirmation 1 | Volume expansion candle (>3x avg) |
| Confirmation 2 | CVD spike in breakout direction |
| Confirmation 3 | Close beyond range with >50% candle body |
| Direction | Direction of breakout |
| Stop Loss | Mid-range, max 0.5% from entry |
| Take Profit | Measured move (range height × 2 minimum) |
| Min R:R | 1:5 |

### 6.3 Confluence Scoring System

Each candidate setup receives a confluence score (0-10). Trade only executes if score ≥ 7.

```
Confluence factors (1 point each unless noted):
  +2  Setup pattern matched cleanly
  +1  Higher timeframe (4H) alignment
  +1  Volume confirmation
  +1  CVD confirmation
  +1  Funding rate alignment
  +1  Liquidation cluster as target
  +1  No high-impact news within 60 minutes
  +1  Within preferred session window
  +1  AMT phase = 'expansion'
```

### 6.4 Optional AI Confluence Layer

For traders who want LLM integration, an AI agent (CrewAI or direct LLM call) can serve as a final reality check. The LLM receives the signal object and confluence score, and either approves or rejects the trade with reasoning. This adds 5-15 seconds of latency, acceptable on 1m timeframe.

> **Recommended pattern:** Rules-based logic generates candidates. LLM serves as a final 'second opinion' veto. LLM never originates trades — it only filters them. This minimizes hallucination risk while adding contextual judgment.

### 6.5 Output Format

```
TradeIntent {
  symbol: 'BTCUSDT',
  direction: 'long' | 'short',
  setup_name: 'value_area_absorption',
  entry_price: 67500.0,
  stop_loss: 67320.0,
  take_profit_1: 67950.0,
  take_profit_2: 68400.0,
  confluence_score: 8,
  confluences: ['absorption', 'cvd_div', 'funding'],
  expected_rr: 5.0,
  timestamp: 1715958060000
}
```

---

## 7. Layer 4 — Risk Management (Critical Safety Layer)

> **THIS LAYER HAS VETO POWER OVER EVERYTHING.** The Strategy Engine cannot bypass Risk Management. Even if the AI strongly recommends a trade, if any risk rule fails, the trade is REJECTED. No exceptions, no overrides except by the human operator via authenticated command.

### 7.1 Hard-Coded Limits (Cannot Be Bypassed)

| Limit | Value |
|---|---|
| Max risk per trade | 0.5% of account equity |
| Max concurrent positions | 1 (single position only) |
| Max trades per day | 5 |
| Max consecutive losses | 2 (then 60-min cooldown) |
| Daily loss limit | 3% of account equity |
| Weekly loss limit | 8% of account equity |
| Max leverage | 10x |
| Margin mode | ISOLATED (always) |
| Position notional cap | 5x account equity (regardless of leverage) |
| Min R:R for entry | 1:4 |
| Max position duration | 4 hours (force close after) |

### 7.2 Position Sizing Calculation

```python
def calculate_position_size(account_equity, risk_pct, entry, sl):
    risk_usdt = account_equity * (risk_pct / 100)
    sl_distance_pct = abs(entry - sl) / entry
    position_notional = risk_usdt / sl_distance_pct
    # Apply caps
    max_notional = account_equity * 5
    position_notional = min(position_notional, max_notional)
    return position_notional
```

### 7.3 Kill Switch Triggers

System enters HALT state (no new trades, existing positions managed normally) when ANY of:

- Daily loss limit hit
- Weekly loss limit hit
- 3 consecutive losses (even within day)
- Data feed interrupted > 30 seconds
- Binance API error rate > 5% over 1 minute
- Account equity drops below $50 floor
- Unrealized PnL on open position drops below 2x intended max loss (slippage emergency)
- Manual halt command received via Telegram

### 7.4 News Event Blackout

System enters HALT state 15 minutes before AND 15 minutes after high-impact news events:

- US CPI release
- FOMC meetings and minutes
- US NFP (Non-Farm Payrolls)
- US GDP releases
- Crypto-specific: ETF flow data, major regulatory announcements

Integrate with economic calendar API (e.g., Trading Economics, ForexFactory).

### 7.5 Black Swan Protection

> Black swan events have wiped algorithmic accounts. Implement these protections without exception:

- Hard stop-loss orders placed AT exchange (not just monitored locally)
- Reduce-only mode after kill switch — system can only close, never open
- Position liquidation distance must be >= 3x stop-loss distance
- Daily snapshot of account state for forensic analysis
- Telegram alert on EVERY order placement and fill
- Heartbeat monitoring — alert if system unresponsive >60 seconds

---

## 8. Layer 5 — Execution Engine

### 8.1 Responsibilities

- Place approved trade intents on Binance
- Manage active positions (SL adjustments, partial TPs)
- Handle order errors, retries, and partial fills
- Maintain state consistency between local DB and exchange
- Reconcile positions on system restart

### 8.2 Order Placement Strategy

#### 8.2.1 Entry Orders

Use LIMIT orders for entries to capture maker rebates and avoid slippage. Set a 10-second timeout — if not filled, cancel and re-evaluate.

```python
# Pseudocode
entry_order = await client.futures_create_order(
    symbol='BTCUSDT',
    side='BUY',
    type='LIMIT',
    quantity=position_size,
    price=entry_price,
    timeInForce='GTC',
    reduceOnly=False,
    newClientOrderId=generate_uuid()
)
```

#### 8.2.2 Stop Loss Orders

Place IMMEDIATELY after entry fill confirmation. Use STOP_MARKET to guarantee execution (no slippage protection but guaranteed close).

```python
sl_order = await client.futures_create_order(
    symbol='BTCUSDT',
    side='SELL',  # opposite of entry
    type='STOP_MARKET',
    stopPrice=stop_loss_price,
    closePosition=True,
    workingType='MARK_PRICE'  # protects from wick liquidations
)
```

#### 8.2.3 Take Profit Orders

Use LIMIT orders for TPs. Split into 2-3 partial closes for scaling out. Move SL to breakeven after TP1 hit.

### 8.3 Position Management Logic

- After +0.3% favorable move → move SL to breakeven
- After TP1 hit (50% closed) → move SL to +0.2% (lock profit)
- After TP2 hit (25% closed) → trail SL using 1m swing structure
- If position duration exceeds 4 hours → force close at market
- If funding rate flips adversely > 0.05% → tighten SL by 50%

### 8.4 State Reconciliation

On startup or after disconnect, the system MUST reconcile state:

1. Query account for all open positions and orders
2. Compare against local database state
3. If mismatch: HALT and alert operator immediately
4. Do NOT auto-resolve mismatches — human verification required

### 8.5 Error Handling

| Error Type | Response | Alert Level |
|---|---|---|
| Insufficient margin | Reject trade, log, continue | Warning |
| Symbol filter violation | Round to valid params, retry | Info |
| Rate limit hit | Exponential backoff | Warning |
| Network timeout | Retry 3x, then HALT | Critical |
| Order rejected | Log details, HALT, alert | Critical |
| Partial fill on entry | Place SL for filled portion only | Warning |
| SL placement failure | EMERGENCY: close position at market | Critical |

---

## 9. Layer 6 — Learning & Analytics

### 9.1 Trade Journal Database Schema

Every trade is recorded with full context for post-hoc analysis. Schema:

```
Table: trades
  id                  UUID PRIMARY KEY
  timestamp_entry     TIMESTAMP
  timestamp_exit      TIMESTAMP
  symbol              VARCHAR(20)
  direction           VARCHAR(10)  -- long/short
  setup_name          VARCHAR(50)
  entry_price         DECIMAL(20,8)
  exit_price          DECIMAL(20,8)
  stop_loss           DECIMAL(20,8)
  take_profit         DECIMAL(20,8)
  position_size       DECIMAL(20,8)
  leverage            INT
  pnl_usdt            DECIMAL(20,8)
  pnl_pct             DECIMAL(10,4)
  fees_usdt           DECIMAL(20,8)
  rr_realized         DECIMAL(6,2)
  rr_planned          DECIMAL(6,2)
  result              VARCHAR(20)  -- win/loss/breakeven
  confluence_score    INT
  confluences         JSONB
  signal_snapshot     JSONB
  market_phase        VARCHAR(20)
  notes               TEXT
```

### 9.2 Performance Metrics

Calculate and store daily, weekly, monthly:

- Win rate (overall and per setup)
- Average win / average loss
- Profit factor (gross profit / gross loss)
- Sharpe ratio (annualized)
- Maximum drawdown (peak-to-trough)
- Expectancy per trade
- R-multiple distribution
- Best/worst trading hours
- Best/worst setups by expectancy
- Fee impact analysis

### 9.3 Learning Loop Architecture

The system 'learns' through three mechanisms:

#### 9.3.1 Parameter Tuning (Automated, Conservative)

Weekly analysis runs that may suggest parameter adjustments (presented to human for approval, never auto-applied):

- Setup-specific win rates over rolling 30 trades
- Confluence factors with highest correlation to wins
- Optimal stop-loss distances per setup
- Time-of-day filtering recommendations

#### 9.3.2 ML Model Retraining (Optional Advanced Feature)

If using ML for confluence scoring, retrain weekly on new trade data. Use walk-forward validation to prevent overfitting.

#### 9.3.3 Human Review Loop

Weekly review report delivered every Sunday containing:

- All trades with screenshots (chart at entry, chart at exit)
- Mistakes identified (rule violations, edge cases)
- Suggested parameter adjustments
- Equity curve and drawdown chart

> The system 'learns' but the trader approves changes. Fully autonomous self-modification is dangerous — it can drift into unprofitable territory. Human approval gates protect against this.

---

## 10. Layer 7 — Monitoring & User Interface

### 10.1 Telegram Bot (Primary Interface)

Telegram is recommended over web UI for real-time alerts and commands. Mobile-native, instant notifications, simple bidirectional commands.

#### 10.1.1 Outbound Alerts

- Trade opened: setup, direction, size, SL, TP, confluence score
- Trade closed: result, PnL, fees, R-multiple realized
- SL/TP modifications: new levels and reasons
- Daily summary: trades count, win rate, PnL, drawdown
- Risk alerts: approaching daily loss limit, consecutive losses
- System alerts: API errors, data feed issues, HALT triggers
- Critical alerts: kill switch activated, position liquidated

#### 10.1.2 Inbound Commands

| Command | Action |
|---|---|
| /status | Current account state, open positions, today's PnL |
| /halt | Stop all new trades immediately |
| /resume | Resume trading (requires confirmation) |
| /close_all | Close all positions at market (requires confirmation) |
| /stats [period] | Performance stats (today/week/month/all) |
| /last [n] | Last N trades with details |
| /risk | Show current risk parameters |
| /setup [name] | Show setup definition and stats |

### 10.2 Web Dashboard (Optional)

For deeper analytics, a web dashboard built with Grafana (over Prometheus metrics) or custom React app:

- Real-time equity curve
- Drawdown visualization
- Trade markers on price chart
- Confluence factor heatmap
- Setup performance comparison
- Recent logs viewer

### 10.3 Logging Standards

Structured JSON logging with the following levels:

| Level | When to Use |
|---|---|
| DEBUG | Detailed signal calculations, indicator values |
| INFO | Trade decisions, setup matches, normal operations |
| WARNING | Recoverable issues, near-limit conditions |
| ERROR | Order failures, API errors, data issues |
| CRITICAL | Kill switch triggers, emergency closes, system halts |

Retain logs for minimum 90 days. Ship to centralized log service (Loki, CloudWatch, or simple file rotation).

---

## 11. Security Requirements

### 11.1 API Key Management

> This is the #1 attack vector. A compromised API key with withdrawal permission = total account loss within seconds. Multiple bot operators have lost everything to leaked keys. Take this section as seriously as the trading logic itself.

- Binance API key MUST have 'Enable Futures' permission ONLY
- Binance API key MUST NOT have 'Enable Withdrawals' permission
- Binance API key MUST have IP whitelist set to VPS IP only
- Keys stored in environment variables, never committed to git
- Use secret management service (AWS Secrets Manager, HashiCorp Vault) for production
- Rotate keys quarterly minimum
- Separate keys for staging vs production

### 11.2 Server Hardening

- SSH access via key only, password authentication disabled
- Firewall: only ports 22 (SSH, IP-restricted), 443 (HTTPS for monitoring) open
- Fail2ban configured for SSH brute force protection
- Automatic security updates enabled
- No root login allowed
- Non-privileged user for application processes
- File permissions: 600 for config files, 700 for directories

### 11.3 Code Security

- Input validation on ALL external data (especially exchange responses)
- Type checking with mypy in production code
- Dependency scanning with safety or snyk
- Code review required for risk management and execution logic changes
- Signed commits for production branch

### 11.4 Operational Security

- 2FA on Binance account itself
- Anti-phishing code set on Binance
- Withdrawal whitelist enabled on Binance
- Email alerts on any Binance login from new IP
- Regular backups of trade database (encrypted)
- Disaster recovery plan documented

---

## 12. Development Phases & Timeline

### 12.1 Phase Breakdown

#### Phase 1: Foundation (Weeks 1-3)

- Set up development environment, repository, CI/CD
- Implement Layer 1: Data ingestion with WebSocket + REST
- Implement Layer 2: Core indicators (CVD, Volume Profile, basic patterns)
- Unit tests for indicator calculations
- Historical data backfill capability

#### Phase 2: Strategy & Backtesting (Weeks 4-6)

- Implement Layer 3: Strategy engine with 3 setups
- Build event-driven backtesting framework
- Backtest on 12+ months of historical data
- Walk-forward validation
- Document performance metrics per setup

#### Phase 3: Risk & Execution (Weeks 7-9)

- Implement Layer 4: Risk management with all hard limits
- Implement Layer 5: Execution engine with Binance integration
- Integration tests on Binance testnet
- Failure mode testing (network drops, API errors, etc.)

#### Phase 4: Monitoring & Learning (Weeks 10-11)

- Implement Layer 6: Trade journal and analytics
- Implement Layer 7: Telegram bot, alerts, dashboards
- Logging infrastructure
- Documentation

#### Phase 5: Paper Trading (Weeks 12-16)

- Deploy to production server in dry-run mode
- Run for minimum 4 weeks with no real capital
- Compare paper results to backtest expectations
- Fix any production-only issues discovered

#### Phase 6: Micro Live (Weeks 17-24)

- Deploy with minimal capital ($100-200)
- Trade ONE setup only initially
- Monitor every trade manually for first 2 weeks
- Gradual expansion to all setups
- Weekly performance reviews

#### Phase 7: Scaling (Weeks 25+)

- Only after proven profitability over 8+ weeks live
- Gradual capital increase
- Add additional pairs if architecture supports
- Continuous improvement based on data

### 12.2 Total Estimated Timeline

Full system from concept to scaled deployment: **6-9 months** for an experienced Python developer with trading domain knowledge. For developers learning trading concepts in parallel, expect **9-15 months**.

---

## 13. Cost Estimates

### 13.1 Development Costs

| Path | Estimated Cost |
|---|---|
| Self-build (your time) | $0 cash + 800-1200 hours |
| Freelance developer (mid-level) | $8,000 - $25,000 |
| Senior freelance developer | $25,000 - $60,000 |
| Development agency | $50,000 - $150,000 |

### 13.2 Monthly Operating Costs

| Item | Monthly Cost |
|---|---|
| VPS hosting (AWS/DigitalOcean) | $20 - $80 |
| Database (managed PostgreSQL) | $15 - $50 |
| Coinglass API (premium) | $0 - $39 |
| LLM API costs (if using AI layer) | $30 - $300 |
| Monitoring (Grafana Cloud free tier) | $0 - $50 |
| Telegram bot hosting | $0 |
| Backup storage | $5 - $20 |
| **TOTAL** | **$70 - $540** |

### 13.3 Trading Costs

| Cost Type | Detail |
|---|---|
| Binance maker fee | 0.02% (with VIP discount lower) |
| Binance taker fee | 0.05% |
| Funding fees | Variable, typically ±0.01% every 8h |
| Slippage estimate | 0.01-0.05% per trade |
| Per round trip total | ~0.07-0.15% |

> Trading costs are deterministic; profit is probabilistic. Account for ~$15-30 per 100 trades on a $1000 account in fees alone. This is why edge must be statistically significant — small edges get destroyed by fees.

---

## 14. Success Criteria & Go/No-Go Gates

### 14.1 Backtesting Gate

Before progressing to paper trading, system MUST achieve:

- Profit factor > 1.5 over 12+ months of backtests
- Maximum drawdown < 20%
- Sharpe ratio > 1.0 (annualized)
- Minimum 200 trades in backtest
- Consistent across different market regimes (bull, bear, chop)

### 14.2 Paper Trading Gate

Before progressing to live capital, system MUST achieve over 4+ weeks paper trading:

- Live results within 30% of backtest expectations
- No critical failures or unexpected behaviors
- Risk limits never breached
- Reliable uptime > 99%

### 14.3 Live Trading Continuation Criteria

Continue live trading ONLY if:

- Profitable over rolling 30-trade windows
- Drawdown stays under 15% from peak
- Win rate within 10% of backtest expectations
- No system errors causing capital loss
- Trader's psychological state remains stable

> If ANY of these criteria fail for 2+ consecutive review periods, STOP. Revert to paper trading or refactor. Continuing to trade a failing system is the fastest way to ruin.

---

## 15. Risk Register

| Risk | Mitigation | Severity |
|---|---|---|
| Strategy edge decay over time | Continuous monitoring, periodic review | High |
| API key compromise | Strict permissions, IP whitelist, 2FA | Critical |
| Exchange outage during open position | Stop orders at exchange, position size limits | High |
| Flash crash slippage | Smaller positions, mark price stops | High |
| Code bug causes wrong trade | Extensive testing, gradual rollout, kill switch | Critical |
| VPS failure mid-trade | Stops at exchange (not local), failover plan | Medium |
| Regulatory changes | Monitor jurisdiction, multiple exchange contingency | Medium |
| LLM hallucination (if using AI) | LLM as veto only, never as originator | Medium |
| Overfitting to historical data | Walk-forward validation, regime testing | High |
| Trader override during drawdown | Hard-coded limits, no manual override | Medium |
| Funding rate spikes | Auto-exit conditions, position duration limits | Medium |
| Black swan event | Conservative sizing, isolated margin always | Critical |

---

## 16. Alternative Path: Build on Freqtrade

### 16.1 Why Consider Freqtrade

If building from scratch is too time-intensive, Freqtrade provides 80% of this architecture out of the box. It is the most mature open-source crypto trading framework, with 25,000+ GitHub stars and active development.

#### 16.1.1 What Freqtrade Provides

- **Layer 1 (Data Ingestion):** Built-in, supports Binance Futures
- **Layer 4 (Risk Management):** Configurable risk limits
- **Layer 5 (Execution):** Battle-tested order management
- **Layer 6 (Analytics):** Sqlite-based trade database
- **Layer 7 (Monitoring):** Telegram bot built-in
- Backtesting framework
- Hyperopt for parameter tuning
- FreqAI module for ML integration

#### 16.1.2 What You Still Need to Build

- **Layer 2 (Signal Processing):** Custom CVD, Volume Profile, absorption logic
- **Layer 3 (Strategy Engine):** Fabervaale-specific setup logic
- Custom indicators for order flow concepts
- Integration with Coinglass/external data

### 16.2 Recommended Approach

Start with Freqtrade as the foundation. Build Fabervaale-style indicators and strategies as Freqtrade strategy files. This cuts development time by 60-70% while leveraging battle-tested infrastructure.

> If you're handing this to a developer, recommend they evaluate Freqtrade first. A senior developer can have a Freqtrade-based system in dry-run within 2-3 weeks. A fully custom system takes 3-6 months. Both can implement the same strategy.

---

## 17. Developer Requirements

### 17.1 Required Skills

- Python 3.10+ (advanced): async/await, type hints, decorators, OOP
- REST APIs and WebSockets: production-grade integration experience
- Database design: PostgreSQL, indexing, transactions
- Testing: pytest, mocking external services
- Linux server administration: SSH, systemd, Docker
- Git workflow: feature branches, code review, CI/CD

### 17.2 Highly Recommended Skills

- Cryptocurrency trading domain knowledge
- Time-series data handling (pandas)
- Quantitative finance basics
- Statistical analysis
- ML/AI: scikit-learn, PyTorch (if AI layer wanted)

### 17.3 Soft Requirements

- Risk-aware mindset (this is financial software, bugs cost money)
- Strong communication for reviewing trades and discussing changes
- Available for monitoring during initial deployment
- Comfortable working with sensitive credentials

### 17.4 Questions to Ask a Developer

1. Have you built trading systems before? Can you share details (NDA-friendly)?
2. How do you handle race conditions in order placement?
3. How do you test code that interacts with live financial APIs?
4. What's your approach to handling exchange rate limits?
5. How do you secure API credentials in production?
6. Describe a time your code caused a financial loss — what did you learn?
7. How would you design the kill switch in this system?
8. What's your experience with Binance Futures specifically?

---

## 18. Appendix: Quick Reference

### 18.1 Key Numbers (Memorize These)

| Parameter | Value |
|---|---|
| Max risk per trade | 0.5% |
| Min R:R required | 1:4 |
| Max leverage | 10x |
| Default leverage | 5x |
| Margin mode | ISOLATED always |
| Max trades per day | 5 |
| Daily loss limit | 3% |
| Weekly loss limit | 8% |
| Max concurrent positions | 1 |
| Max position duration | 4 hours |
| News blackout window | ±15 min around high-impact |

### 18.2 Glossary

- **CVD:** Cumulative Volume Delta — running sum of buy minus sell volume
- **POC:** Point of Control — price with highest volume in profile
- **VAH / VAL:** Value Area High / Low — boundaries of 70% volume zone
- **ChoCH:** Change of Character — first structural break in opposite direction
- **BOS:** Break of Structure — continuation break in trend direction
- **AMT:** Auction Market Theory — framework for market phases
- **OI:** Open Interest — total outstanding contracts
- **Funding Rate:** Periodic payment between longs/shorts on perpetuals
- **Liquidation:** Forced position closure when margin insufficient
- **Absorption:** Passive orders eating aggressive market orders

### 18.3 Reference Materials

- Binance Futures API Documentation: binance-docs.github.io/apidocs/futures/en/
- python-binance Documentation: python-binance.readthedocs.io
- Freqtrade Documentation: www.freqtrade.io
- ccxt Documentation: docs.ccxt.com
- Coinglass API: coinglass.github.io/API-Reference

### 18.4 Final Notes for Developer Handoff

This specification is the blueprint, not the implementation. A skilled developer will translate these requirements into working code, but they should ALSO challenge specifications that seem incorrect or impractical. The strategy logic in particular requires deep trading domain knowledge — a developer without this background should pair with a trader during Phase 2-3.

**Prioritize ruthlessly:**

1. **Risk management (Layer 4)** is the most critical layer. Bugs here cost money directly.
2. **Execution (Layer 5)** is second most critical. Wrong orders are catastrophic.
3. **Data ingestion (Layer 1)** is third. Bad data leads to bad decisions.
4. **Strategy engine (Layer 3)** is fourth. Wrong here causes losses but not blowups.
5. Everything else can be built incrementally.

---

**— END OF SPECIFICATION —**

*This document represents a starting point for serious engineering work. It is not exhaustive — implementation will reveal additional requirements and edge cases. Review and revise as the project progresses.*
