# Trading on Hyperliquid with your wallet

Maya's default venue is **Hyperliquid** — on-chain perpetual futures. You trade
from your own wallet, but the bot signs with a separate **agent (API) wallet**
that can place orders yet **cannot withdraw your funds**. So the autonomous bot
never touches your seed phrase or withdrawal key.

> ⚠️ **Security first.** Never paste your main seed phrase or private key
> anywhere — not in `.env`, not in chat, not in code. Only the *agent key* goes
> in `.env`. Fund a dedicated wallet with only what you're willing to risk.
> Start on **testnet**.

## How it works

```
Your main wallet (holds funds, can withdraw)
        │  approves once
        ▼
Agent / API wallet (signs orders, CANNOT withdraw)  ◄── the bot uses this
```

- **Market data** (candles, prices) comes from Hyperliquid's public API — no key.
- **Orders** are signed by the agent key and placed on your main account.
- **Withdrawals** are impossible with the agent key, by design.

## Step 1 — get testnet funds

1. Go to **https://app.hyperliquid-testnet.xyz** and connect your wallet (MetaMask).
2. Use the faucet to get testnet USDC.

## Step 2 — create an agent (API) wallet

**Option A — in the app (easiest):**
1. On app.hyperliquid-testnet.xyz → **More → API** (or **API Wallets**).
2. Generate an API wallet. It shows a **private key** — this is your
   `HYPERLIQUID_AGENT_KEY`. Approve/authorize it.
3. Your connected wallet address is `HYPERLIQUID_ACCOUNT_ADDRESS`.

**Option B — with the SDK (programmatic):**
```python
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

main = Account.from_key(MAIN_KEY)          # run this ONCE, locally, offline
ex = Exchange(main, constants.TESTNET_API_URL)
approve_result, agent_key = ex.approve_agent()   # agent_key -> HYPERLIQUID_AGENT_KEY
print("account:", main.address, "agent_key:", agent_key)
```
(Only do Option B on a secure, offline machine, and discard the main key after.)

## Step 3 — configure

```ini
MAYA_VENUE=hyperliquid
MAYA_MODE=testnet                 # testnet first!
MAYA_SYMBOL=BTCUSDT               # coin derived as BTC
HYPERLIQUID_ACCOUNT_ADDRESS=0xYourMainWalletAddress
HYPERLIQUID_AGENT_KEY=0xYourAgentPrivateKey
```

## Step 4 — verify the connection

```bash
python -m maya --check
```

A healthy check prints the last BTC mid price and your account equity. If it
fails, the output tells you whether it's a network block or a missing SDK.

## Step 5 — run

```bash
python -m maya        # trades on Hyperliquid testnet, autonomously
```

Position sizing, the 0.5%-risk rule, kill switch, news blackout, scaling TPs and
Telegram alerts all work exactly as on the Binance venue — only the data feed
and order routing change.

## Going to mainnet

Only after a profitable backtest (`python -m maya.backtest --source csv ...`)
and a solid testnet run:

```ini
MAYA_MODE=live
MAYA_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK
```

`live` uses Hyperliquid **mainnet (real money)** and is refused unless the
interlock string is set and the agent key is present.

## Notes & current limitations

- **Egress:** the host must reach `api.hyperliquid.xyz` /
  `api.hyperliquid-testnet.xyz`. Restricted sandboxes block these.
- **Order flow:** Hyperliquid klines carry no per-trade data, so CVD is
  *approximated* from each candle's close position (same as the CSV backtester).
  A future upgrade can aggregate the websocket `trades` feed for true CVD.
- **Backtesting:** pull real Hyperliquid history straight into the backtester:
  ```bash
  python -m maya.backtest --source hyperliquid --days 365 --walk-forward 5
  ```
  (Uses mainnet candles. Needs network access to Hyperliquid.)
