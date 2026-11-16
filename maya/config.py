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
    # Candles to process before stopping; 0 = run until stopped. Unset, it is
    # 200 for the simulator (a quick demo) and unlimited whenever real market
    # data is involved: a fixed 200 used to stop paper, testnet and LIVE runs
    # after 200 candles (3h20m on 1m) and flatten the open position on exit.
    max_candles: int | None = None
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
    # "risk_pct": size so that a stop-out loses ``risk_per_trade_pct`` of
    #            current equity: (equity x pct) / stop distance, clamped by
    #            the notional cap. The mode for small accounts, where a fixed
    #            dollar amount is the wrong unit.
    sizing_mode: str = "margin"
    margin_per_trade_usd: float = 20.0
    risk_per_trade_usd: float = 20.0
    risk_per_trade_pct: float = 3.0

    # Leverage actually used for orders. Bounded by RiskLimits.MAX_LEVERAGE.
    leverage: int = 10

    # -- profit protection -------------------------------------------------- #
    # Once price has moved this far in favour (percent of entry, on a close),
    # the stop goes to entry plus the round-trip fees, so a reversal from
    # there costs nothing. After that it trails ``profit_trail_pct`` behind
    # the best price seen. A stop only ever moves towards profit.
    profit_lock_trigger_pct: float = 0.6
    profit_trail_pct: float = 0.3

    # -- Kronos forecast input (unmeasured, so off) -------------------------- #
    # One extra reading on the SignalObject, computed when a bar of
    # ``kronos_timeframe`` closes. Needs a clone of github.com/shiyu-coder/Kronos
    # at ``kronos_repo_path`` with its requirements installed.
    kronos_enabled: bool = False
    kronos_repo_path: str = ""
    kronos_timeframe: str = "15m"
    kronos_lookback: int = 256          # bars fed to the model, at most 512
    kronos_pred_len: int = 4            # bars forecast ahead
    kronos_samples: int = 3             # sampled paths averaged per forecast
    kronos_temperature: float = 1.0
    kronos_top_p: float = 0.9
    kronos_margin_bps: float = 5.0      # safety margin on top of round-trip cost
    kronos_cache_path: str = "data/kronos_cache.db"
    kronos_device: str = ""             # blank lets the predictor choose

    # -- paper entry simulation --------------------------------------------- #
    # "immediate": a limit entry fills at once at the intent price (the old
    #              behaviour, and the optimistic case).
    # "post_only": the order rests at the entry price and fills only if a
    #              later candle trades through it, and then only with
    #              ``maker_fill_probability``: trading through a level does
    #              not mean an order at the back of the queue was reached.
    # Sim, paper and backtest only. Real-order brokers ignore this.
    paper_entry_mode: str = "immediate"
    maker_fill_probability: float = 0.6
    # Candles a resting order waits before it is cancelled and counted missed.
    maker_order_ttl_candles: int = 1
    # Candles after the order was placed at which its outcome is marked, for
    # comparing filled orders with missed ones.
    maker_adverse_horizon_candles: int = 5
    maker_seed: int = 7

    # -- multi-timeframe layers (both unmeasured, so both off) -------------- #
    # Bias filter: a setup may only fire in the direction of the 15m bias
    # (price against session VWAP plus swing structure). No bias, no trade.
    mtf_bias_filter_enabled: bool = False
    # 15s trigger: the entry also needs the last 15-second bar of the closing
    # minute to confirm. Needs tick data, which only the websocket feed
    # provides; without ticks no trade is taken while this is on.
    mtf_15s_trigger_enabled: bool = False

    # -- lessons memory ----------------------------------------------------- #
    # Before an entry, past trades of the same setup and direction whose
    # confluence factors overlap by at least ``lessons_similarity`` (Jaccard)
    # are looked up. With ``lessons_min_matches`` or more of them and a win
    # rate under ``lessons_min_win_rate``, the entry is vetoed. 0.2 is the
    # break-even win rate of a 1:4 trade before fees. Veto only: this can
    # never approve a trade or change a limit.
    lessons_veto_enabled: bool = True
    lessons_similarity: float = 0.75
    lessons_min_matches: int = 5
    lessons_min_win_rate: float = 0.2

    # Minimum expected ROI **on margin** (i.e. the price move multiplied by
    # leverage), not the raw price move — see TradeIntent.roi_on_margin_pct.
    #
    # Read this as a price move before changing it: at the default 10x, 10%
    # on margin is a 1% move to TP1, which is a real but reachable 1m scalp.
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
    # Per-venue fee model (see maya/fees.py). Blank = venue defaults:
    # Hyperliquid tier 0 maker 1.5 / taker 4.5bps plus MetaMask's builder fee,
    # assumed at Hyperliquid's 10bps cap until `mm perps quote` reports the
    # real figure; Binance VIP 0 maker 2 / taker 5bps, no builder fee.
    fee_maker_bps: float | None = None
    fee_taker_bps: float | None = None
    builder_fee_bps: float | None = None
    # Fee-aware entry gate. A trade must still pay after the round trip:
    # net R:R = (target move - fees) / (stop move + fees) must reach this, and
    # fees may take at most `max_fee_share` of the move to TP1.
    min_net_rr: float = 2.5
    max_fee_share: float = 0.20
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

    # Setup 3 (compression breakout) was unreachable until it was rebuilt to
    # spec, so it has never been measured, and it confirms longs with the CVD
    # sign the README's 24-month study found to point the wrong way. Off by
    # default so live behaviour matches what was measured; turn on only after
    # `maya.backtest.sweep` shows it paying after fees.
    compression_breakout_enabled: bool = False
    # Adds the two flipped-CVD setups (maya.strategy.setups.CVD_FLIP_SETUPS).
    # Unmeasured, so off.
    cvd_flip_setups_enabled: bool = False
    # Comma-separated setup names to run and nothing else. Blank means the
    # normal selection. For research runs that isolate one idea.
    setups_only: str = ""

    # -- Multi-coin scanner (maya.scanner / maya.scan_run) ------------------ #
    # Coins come from Hyperliquid's public market list, busiest first.
    scan_max_coins: int = 15
    scan_min_volume_usd: float = 20_000_000.0
    # Closed candles replayed per coin to warm up CVD, swings and HTF context.
    scan_lookback: int = 400
    # Comma-separated pin (e.g. "BTC,ETH,SOL"), bypassing the volume filter.
    scan_symbols: str = ""
    # Let the chart desk (Atlas/Echo/Flow) veto a setup it leans against.
    scan_desk_veto: bool = True

    # LLM veto layer
    llm_enabled: bool = False
    llm_model: str = "claude-opus-5-5"
    # A veto is a short yes/no judgement; low effort keeps it fast and cheap.
    llm_effort: str = "low"

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

    @field_validator("fee_rate", "fee_maker_bps", "fee_taker_bps", "builder_fee_bps",
                     "max_candles", mode="before")
    @classmethod
    def _blank_means_unset(cls, v):
        """``MAYA_BUILDER_FEE_BPS=`` in a .env means "use the default", not a
        crash on startup."""
        return None if isinstance(v, str) and not v.strip() else v

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
        if v not in ("margin", "risk", "risk_pct"):
            raise ValueError(f"sizing_mode={v!r} must be 'margin', 'risk' or 'risk_pct'")
        return v

    @field_validator("paper_entry_mode")
    @classmethod
    def _known_entry_mode(cls, v: str) -> str:
        if v not in ("immediate", "post_only"):
            raise ValueError(f"paper_entry_mode={v!r} must be 'immediate' or 'post_only'")
        return v

    @field_validator("maker_fill_probability")
    @classmethod
    def _fill_probability_in_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("maker_fill_probability must be in (0, 1]")
        return v

    @field_validator("risk_per_trade_pct")
    @classmethod
    def _risk_pct_within_hard_limit(cls, v: float) -> float:
        if v <= 0 or v > RiskLimits.MAX_RISK_PER_TRADE_PCT:
            raise ValueError(
                f"risk_per_trade_pct={v} outside (0, {RiskLimits.MAX_RISK_PER_TRADE_PCT}] "
                "(RiskLimits.MAX_RISK_PER_TRADE_PCT)")
        return v

    @field_validator("profit_lock_trigger_pct", "profit_trail_pct")
    @classmethod
    def _profit_protection_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("profit protection distances must be above zero")
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
    def candle_limit(self) -> int:
        """How many candles to run; 0 means until stopped."""
        if self.max_candles is not None:
            return max(self.max_candles, 0)
        return 200 if self.mode is RunMode.SIM else 0

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
        """Average per-side cost of a worst-case round trip (fees + builder).

        Kept for callers that think in one per-side number (the Monte Carlo
        model). PnL itself is charged leg by leg — see :mod:`maya.fees`.
        """
        if self.fee_rate is not None:
            return self.fee_rate
        from maya.fees import FeeModel

        return FeeModel.for_settings(self).worst_round_trip(self.perps_order_type) / 2

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

    # Both ceilings apply to every sizing mode; the smaller one wins. The
    # percentage is what binds on a small account ($0.30 on $10), the USD
    # figure on a large one. Raised from 0.5% at the owner's request so a $10
    # account at 10x can size a trade at all: ten straight losses at 3% is a
    # 26% drawdown, which is why DAILY_LOSS_LIMIT_PCT below is the real brake.
    MAX_RISK_PER_TRADE_PCT = 3.0          # % of equity
    MAX_RISK_PER_TRADE_USD = 20.0        # fixed USD risk per trade
    MAX_CONCURRENT_POSITIONS = 1
    MAX_TRADES_PER_DAY = 10
    MAX_CONSECUTIVE_LOSSES = 2            # then cooldown
    COOLDOWN_MINUTES = 60
    DAILY_LOSS_LIMIT_PCT = 10.0
    # Left at 8% on purpose: nobody asked for it to move. It is now tighter
    # than the daily limit, so a single 8% day early in the week halts trading
    # until the next week. Raise it only by an explicit decision.
    WEEKLY_LOSS_LIMIT_PCT = 8.0
    MAX_LEVERAGE = 10
    # Maintenance margin assumed when estimating where a position liquidates.
    # Hyperliquid BTC is 1.25% (half the initial margin at its 40x max);
    # Binance BTC tier 1 is 0.4%. The larger figure is used for both venues
    # so the estimate errs towards liquidating *sooner*.
    ASSUMED_MAINTENANCE_MARGIN = 0.0125
    # The stop must sit inside this fraction of the distance to liquidation,
    # leaving room for slippage on the stop fill and accrued funding.
    LIQUIDATION_BUFFER = 0.8
    DEFAULT_LEVERAGE = 5
    POSITION_NOTIONAL_CAP_MULT = 10.0    # x equity
    MIN_RR_FOR_ENTRY = 4.0
    MAX_POSITION_DURATION_HOURS = 4
    EQUITY_FLOOR_USDT = 50.0
    # A flat $50 floor halts a $10 account before its first trade, so the
    # floor is the smaller of $50 and this share of the starting equity.
    EQUITY_FLOOR_FRACTION_OF_START = 0.5

    @classmethod
    def equity_floor(cls, start_equity: float) -> float:
        return min(cls.EQUITY_FLOOR_USDT, start_equity * cls.EQUITY_FLOOR_FRACTION_OF_START)
    MIN_CONFLUENCE_SCORE = 7             # spec section 6.3
    DATA_STALE_SECONDS = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
