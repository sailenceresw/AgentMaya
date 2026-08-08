"""Configuration & hard-coded risk limits.

Settings come from environment variables (and an optional ``.env`` file).
The :class:`RiskLimits` values mirror the "Hard-Coded Limits" table in the
spec (section 7.1) and are intentionally *not* overridable from the
environment — they are constants compiled into the system.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunMode(str, Enum):
    SIM = "sim"          # synthetic data, simulated execution, no keys
    PAPER = "paper"      # real market data, simulated execution
    TESTNET = "testnet"  # exchange testnet (fake money, real sandbox)
    LIVE = "live"        # REAL MONEY


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
    min_roi_pct: float = 40.0

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
    # Each `mm` call spawns Node and may wait on wallet signing/MFA, so this
    # is generous compared to a REST timeout.
    mm_timeout_s: int = 120

    # LLM veto layer
    llm_enabled: bool = False
    llm_model: str = "claude-sonnet-4-6"

    log_level: str = "INFO"

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

    @field_validator("sizing_mode")
    @classmethod
    def _known_sizing_mode(cls, v: str) -> str:
        if v not in ("margin", "risk"):
            raise ValueError(f"sizing_mode={v!r} must be 'margin' or 'risk'")
        return v

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
