"""Configuration & hard-coded risk limits.

Settings come from environment variables (and an optional ``.env`` file).
The :class:`RiskLimits` values mirror the "Hard-Coded Limits" table in the
spec (section 7.1) and are intentionally *not* overridable from the
environment — they are constants compiled into the system.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunMode(str, Enum):
    SIM = "sim"          # synthetic data, simulated execution, no keys
    PAPER = "paper"      # real market data, simulated execution
    TESTNET = "testnet"  # exchange testnet (fake money, real sandbox)
    LIVE = "live"        # REAL MONEY


# Hyperliquid rejects orders below $10 notional (MetaMask Agent Wallet perps
# guide, Sep 2026).
HYPERLIQUID_MIN_NOTIONAL_USD = 10.0
# Minimum-size mode targets this multiple of the minimum, so a mark price that
# moves between sizing and the order landing cannot push it under $10.
MIN_SIZE_HEADROOM = 1.05

# Per-side fee rates. Binance USD-M taker is 5bps (conservative — maker is
# 2bps). Hyperliquid quoted 0.00015 for a limit order via `mm perps quote`.
BINANCE_FEE_RATE = 0.0005
HYPERLIQUID_FEE_RATE = 0.00015


class Venue(str, Enum):
    """Where orders are actually placed.

    ``binance`` is the original path (ccxt, API key/secret, USD-M futures).
    ``hyperliquid`` routes orders through the MetaMask Agent Wallet ``mm``
    CLI instead — self-custodial, no exchange API keys. The two are mutually
    exclusive: a MetaMask wallet cannot place a Binance order, and a Binance
    key cannot sign a Hyperliquid one.
    """

    BINANCE = "binance"
    HYPERLIQUID = "hyperliquid"


class Settings(BaseSettings):
    """User-facing settings, loaded from env / .env."""

    model_config = SettingsConfigDict(
        env_prefix="MAYA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: RunMode = RunMode.SIM
    venue: Venue = Venue.BINANCE
    symbol: str = "BTCUSDT"
    timeframe: str = "1m"
    max_candles: int = 200
    start_equity: float = 1000.0

    # Higher timeframes folded from the 1m stream for chart reading (trend,
    # market structure, S/R). Comma-separated; blank disables HTF analysis and
    # with it the htf_alignment confluence factor.
    htf_timeframes: str = "15m,1h"

    # -- position sizing ---------------------------------------------------- #
    # "margin" : commit ``margin_per_trade_usd`` of collateral at ``leverage``,
    #            so notional = margin x leverage. The loss on a stop-out is
    #            then whatever the stop distance implies, and is additionally
    #            capped at RiskLimits.MAX_RISK_PER_TRADE_USD.
    # "risk"   : size so that a stop-out loses exactly ``risk_per_trade_usd``.
    #            Position size is derived from the stop distance and leverage
    #            affects only how much margin is locked, never the P&L.
    sizing_mode: str = "margin"
    margin_per_trade_usd: float = 20.0
    risk_per_trade_usd: float = 20.0

    # Leverage actually used for orders. Bounded by RiskLimits.MAX_LEVERAGE.
    leverage: int = 20

    # Minimum expected ROI **on margin** (i.e. the price move multiplied by
    # leverage), not the raw price move — see TradeIntent.roi_on_margin_pct.
    #
    # Read this as a price move before changing it: at the default 20x, 10%
    # on margin is a 0.5% move to TP1, which is a real but reachable 1m scalp.
    # The previous default of 40% demanded a **2% move to target on a
    # 1-minute chart** — a swing-trade distance on a scalping timeframe — and
    # rejected essentially every candidate the strategy produced.
    min_roi_pct: float = 10.0

    # When true (and mode==sim), replay scripted textbook setups instead of the
    # random walk — useful for demos and integration tests.
    scenario: bool = False

    # Opt-in real-time WebSocket ingestion instead of REST polling (paper/
    # testnet/live). Off by default — REST (CCXTFeed) is the more-tested path.
    use_websocket: bool = False

    # Live-trading interlock. Must equal the magic string to permit live mode.
    live_confirm: str = ""

    # -- Hyperliquid / MetaMask Agent Wallet (venue=hyperliquid only) ------- #
    # The `mm perps --venue` value. Hyperliquid is the only venue the CLI
    # currently supports, but this is kept configurable rather than hardcoded.
    perps_venue: str = "hyperliquid"
    # "limit" enters at intent.entry_price, preserving the R:R the strategy
    # actually gated on (matches CCXTBroker). "market" fills immediately at
    # whatever the book offers, which silently changes the planned R:R.
    perps_order_type: str = "limit"
    # Hyperliquid rejects sizes with more decimals than the market's
    # szDecimals. 5 is correct for BTC; verify with `mm perps markets` before
    # trading anything else.
    perps_size_decimals: int = 5
    perps_max_slippage_bps: int = 50
    # Per-side fee rate. Left unset it resolves per venue: Binance USD-M taker
    # is 5bps, while Hyperliquid quoted 1.5bps for a limit order
    # (`feeRate: 0.00015` from `mm perps quote`). Using the Binance number on
    # Hyperliquid overstates costs by ~3.3x on every trade.
    fee_rate: float | None = None
    # Each `mm` call spawns Node and may wait on wallet signing/MFA, so this
    # is generous compared to a REST timeout. It must exceed
    # ``mm_wallet_timeout_s`` (validated below) so the CLI gives up on a
    # stalled signing job — and reports why — before Maya kills the process
    # and is left not knowing whether the order went in.
    mm_timeout_s: int = 120
    # Passed as `--wallet-timeout` on every signing command (open, close,
    # modify, cancel): how long the CLI waits on a wallet job such as a Guard
    # Mode MFA approval. The CLI caps this at 600.
    mm_wallet_timeout_s: int = 90
    # The CLI sends Segment analytics and Sentry reports from every call. With
    # them on, a call whose analytics endpoint is slow or unreachable was
    # measured hanging for 100s *after* printing its result; with them off the
    # same call returned in 1.7s. Off by default (sets MM_TELEMETRY_DISABLED=1
    # for the `mm` subprocess only); set true to share usage data with MetaMask.
    mm_telemetry: bool = False
    # A limit entry can come back "resting" instead of "filled". Maya then
    # polls for the fill this long before cancelling the order, so an unfilled
    # entry is never left on the book without a stop. 0 cancels immediately.
    # Rehearsal: validate every entry against the real venue with
    # `mm perps open --dry-run` and `mm perps quote`, but never sign or submit.
    # Positions are tracked locally like paper trading. MetaMask's own advice
    # is to rehearse this way rather than on testnet, which "can't currently
    # be funded through Agent Wallet's funding path".
    perps_dry_run: bool = False
    # Google, email and MetaMask Mobile QR sign-ins each resolve to a
    # *different* wallet address, even for the same email. Set this to the
    # address Maya should trade from and the preflight refuses any other.
    mm_expected_address: str = ""
    # Size every Hyperliquid order at the venue minimum (~$10.50 notional)
    # instead of margin/risk sizing. For first live trades: the whole
    # order lifecycle runs for real, but a stop-out costs cents. Overrides
    # sizing_mode; every other risk check still applies.
    perps_min_size: bool = False
    perps_limit_fill_timeout_s: float = 15.0
    perps_fill_poll_s: float = 3.0
    # MetaMask Pulse (`mm pulse asset --hl-perps-market <coin>`): an
    # AI-written market summary handed to the LLM veto as extra context. Only
    # read when the LLM layer is enabled; needs an authenticated `mm` session.
    pulse_enabled: bool = False
    pulse_ttl_s: int = 900

    # LLM veto layer
    llm_enabled: bool = False
    llm_model: str = "claude-sonnet-4-6"

    log_level: str = "INFO"

    # -- Layer 7 monitoring API / dashboard --------------------------------- #
    # Off by default: this puts a trading system on a network socket, so it
    # should be a deliberate choice rather than something that happens because
    # you ran the agent.
    api_enabled: bool = False
    # Loopback by default. Set 0.0.0.0 to reach it from a phone on the same
    # network — and understand that anything else on that network can too.
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    # Required whenever the API is enabled; the server refuses to start
    # without it rather than defaulting to unauthenticated. Reads MAYA_API_TOKEN
    # via the env_prefix above — no explicit alias, which would otherwise stop
    # the field being set by name in code and tests.
    api_token: str = ""

    # Credentials are read without the MAYA_ prefix.
    binance_api_key: str = Field(default="", validation_alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", validation_alias="BINANCE_API_SECRET")
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")

    @field_validator("leverage")
    @classmethod
    def _leverage_within_hard_limit(cls, v: int) -> int:
        """Refuse out-of-range leverage rather than silently clamping it — a
        config typo that quietly doubles position size is exactly the class of
        bug the hard limits exist to prevent."""
        if v < 1 or v > RiskLimits.MAX_LEVERAGE:
            raise ValueError(
                f"leverage={v} outside 1..{RiskLimits.MAX_LEVERAGE} "
                f"(RiskLimits.MAX_LEVERAGE)"
            )
        return v

    @field_validator("mm_wallet_timeout_s")
    @classmethod
    def _wallet_timeout_within_cli_cap(cls, v: int) -> int:
        if v < 1 or v > 600:
            raise ValueError(f"mm_wallet_timeout_s={v} outside 1..600 (the CLI's cap)")
        return v

    @model_validator(mode="after")
    def _process_outlives_wallet_job(self) -> "Settings":
        if self.mm_timeout_s <= self.mm_wallet_timeout_s:
            raise ValueError(
                f"mm_timeout_s={self.mm_timeout_s} must exceed mm_wallet_timeout_s="
                f"{self.mm_wallet_timeout_s}: otherwise Maya kills `mm` mid-signing "
                "and cannot tell whether the order was submitted")
        return self

    @field_validator("sizing_mode")
    @classmethod
    def _known_sizing_mode(cls, v: str) -> str:
        if v not in ("margin", "risk"):
            raise ValueError(f"sizing_mode={v!r} must be 'margin' or 'risk'")
        return v

    @property
    def htf_list(self) -> list[str]:
        """Higher timeframes to analyse, always strictly above ``timeframe``.

        A "higher" timeframe equal to (or below) the entry timeframe is not a
        second opinion — it is the same series, so ``htf_alignment`` would
        confirm whatever the entry bar already said and score a free point on
        every trade. Configured entries at or below the entry timeframe are
        therefore dropped, and if that leaves nothing, the defaults step up
        from the entry timeframe instead of silently disabling the factor.
        """
        from maya.timeframe import label_for, parse_timeframe

        try:
            base = parse_timeframe(self.timeframe)
        except ValueError:
            base = 1

        out: list[str] = []
        for tf in (t.strip() for t in self.htf_timeframes.split(",")):
            if not tf:
                continue
            try:
                if parse_timeframe(tf) > base:
                    out.append(tf)
            except ValueError:
                continue  # unparseable entries are ignored, not fatal

        if out or not self.htf_timeframes.strip():
            return out  # blank config means "disabled", and stays disabled
        # Everything configured was too low: scale the usual 15x/60x-of-1m
        # relationship up from whatever the entry timeframe actually is.
        return [label_for(base * 4), label_for(base * 16)]

    @property
    def is_live(self) -> bool:
        return self.mode is RunMode.LIVE

    @property
    def uses_real_orders(self) -> bool:
        return self.mode in (RunMode.TESTNET, RunMode.LIVE)

    @property
    def uses_real_data(self) -> bool:
        return self.mode in (RunMode.PAPER, RunMode.TESTNET, RunMode.LIVE)

    @property
    def effective_fee_rate(self) -> float:
        """Per-side fee rate to charge in PnL, resolved per venue when unset."""
        if self.fee_rate is not None:
            return self.fee_rate
        return (HYPERLIQUID_FEE_RATE if self.venue is Venue.HYPERLIQUID
                else BINANCE_FEE_RATE)

    @property
    def min_size_trading(self) -> bool:
        """True when orders are sized at Hyperliquid's minimum notional."""
        return self.venue is Venue.HYPERLIQUID and self.perps_min_size

    @property
    def uses_metamask(self) -> bool:
        """True when orders route through the MetaMask Agent Wallet CLI.

        Note this governs *execution* only. Market data still comes from
        Binance's public klines endpoint (no keys required) even on the
        Hyperliquid venue — see the basis caveat in ``docs/RUNNING.md``.
        """
        return self.venue is Venue.HYPERLIQUID and self.uses_real_orders


class RiskLimits:
    """Hard-coded safety limits (spec section 7.1). Treat as immutable."""

    MAX_RISK_PER_TRADE_PCT = 0.5          # % of equity (legacy/default)
    MAX_RISK_PER_TRADE_USD = 20.0        # fixed USD risk per trade
    MAX_CONCURRENT_POSITIONS = 1
    MAX_TRADES_PER_DAY = 5
    MAX_CONSECUTIVE_LOSSES = 2            # then cooldown
    COOLDOWN_MINUTES = 60
    DAILY_LOSS_LIMIT_PCT = 3.0
    WEEKLY_LOSS_LIMIT_PCT = 8.0
    MAX_LEVERAGE = 20
    # Maintenance margin assumed when estimating where a position liquidates.
    # Hyperliquid BTC is 1.25% (half the initial margin at its 40x max);
    # Binance BTC tier 1 is 0.4%. The larger figure is used for both venues
    # so the estimate errs towards liquidating *sooner*.
    ASSUMED_MAINTENANCE_MARGIN = 0.0125
    # The stop must sit inside this fraction of the distance to liquidation,
    # leaving room for slippage on the stop fill and accrued funding.
    LIQUIDATION_BUFFER = 0.8
    DEFAULT_LEVERAGE = 5
    POSITION_NOTIONAL_CAP_MULT = 5.0     # x equity
    MIN_RR_FOR_ENTRY = 4.0
    MAX_POSITION_DURATION_HOURS = 4
    EQUITY_FLOOR_USDT = 50.0
    MIN_CONFLUENCE_SCORE = 7             # spec section 6.3
    DATA_STALE_SECONDS = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
