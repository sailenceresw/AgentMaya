"""Configuration & hard-coded risk limits.

Settings come from environment variables (and an optional ``.env`` file).
The :class:`RiskLimits` values mirror the "Hard-Coded Limits" table in the
spec (section 7.1) and are intentionally *not* overridable from the
environment — they are constants compiled into the system.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunMode(str, Enum):
    SIM = "sim"          # synthetic data, simulated execution, no keys
    PAPER = "paper"      # real market data, simulated execution
    TESTNET = "testnet"  # Binance futures testnet (fake money, real sandbox)
    LIVE = "live"        # REAL MONEY


class Settings(BaseSettings):
    """User-facing settings, loaded from env / .env."""

    model_config = SettingsConfigDict(
        env_prefix="MAYA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: RunMode = RunMode.SIM
    symbol: str = "BTCUSDT"
    timeframe: str = "1m"
    max_candles: int = 200
    start_equity: float = 1000.0

    # When true (and mode==sim), replay scripted textbook setups instead of the
    # random walk — useful for demos and integration tests.
    scenario: bool = False

    # Live-trading interlock. Must equal the magic string to permit live mode.
    live_confirm: str = ""

    # LLM veto layer
    llm_enabled: bool = False
    llm_model: str = "claude-sonnet-4-6"

    log_level: str = "INFO"

    # Credentials are read without the MAYA_ prefix.
    binance_api_key: str = Field(default="", validation_alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", validation_alias="BINANCE_API_SECRET")
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")

    @property
    def is_live(self) -> bool:
        return self.mode is RunMode.LIVE

    @property
    def uses_real_orders(self) -> bool:
        return self.mode in (RunMode.TESTNET, RunMode.LIVE)

    @property
    def uses_real_data(self) -> bool:
        return self.mode in (RunMode.PAPER, RunMode.TESTNET, RunMode.LIVE)


class RiskLimits:
    """Hard-coded safety limits (spec section 7.1). Treat as immutable."""

    MAX_RISK_PER_TRADE_PCT = 0.5          # % of equity
    MAX_CONCURRENT_POSITIONS = 1
    MAX_TRADES_PER_DAY = 5
    MAX_CONSECUTIVE_LOSSES = 2            # then cooldown
    COOLDOWN_MINUTES = 60
    DAILY_LOSS_LIMIT_PCT = 3.0
    WEEKLY_LOSS_LIMIT_PCT = 8.0
    MAX_LEVERAGE = 10
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
