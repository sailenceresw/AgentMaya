"""Multi-coin scanner — read every liquid chart, trade only what pays after fees.

Instead of watching one symbol and waiting for it to set up, the scanner
walks a universe of liquid perpetuals each time a candle closes and asks the
same questions of every one:

1. **Does the strategy fire here?** Each coin's recent candles run through
   the *same* :class:`~maya.signals.processor.SignalProcessor` and
   :class:`~maya.strategy.engine.StrategyEngine` the single-symbol agent
   uses, so a scanned trade is held to exactly the rules a backtest measured
   — there is no second, scanner-only strategy to drift out of sync.
2. **What does the chart desk say?** Atlas (zones and structure), Echo
   (candlestick patterns at those zones) and Flow (CVD) confer on the coin.
   A desk that leans *against* the trade vetoes it; a neutral desk does not.
   The desk only ever removes candidates: Maya's own measurements found its
   verdict non-predictive on its own (see README), so it is used as a brake,
   never as a reason to trade.
3. **Does it still pay after fees?** The risk manager's fee-aware gate —
   net R:R and fee share of the target, using MetaMask's and Hyperliquid's
   real round trip — screens every candidate.

Survivors are ranked by net R:R after fees, then by confluence score.

**Data — all free, no keys.** The universe and live funding come from
Hyperliquid's public ``info`` endpoint (``metaAndAssetCtxs``), so only coins
that are actually tradable on the venue are scanned, ordered by real 24h
volume. Candles come from Binance USD-M's public klines, because they carry
the taker-buy volume that makes CVD real order flow; Hyperliquid candles do
not. Coins listed on Hyperliquid but not on Binance are skipped and logged.

Run it read-only (any mode with market data)::

    MAYA_MODE=paper MAYA_TIMEFRAME=15m python -m maya.scanner

or let it trade the best candidate — see :mod:`maya.scan_run`.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from maya.agents import desk
from maya.agents.models import BEARISH, BULLISH
from maya.config import Settings, get_settings
from maya.data.models import Candle
from maya.fees import FeeModel, economics
from maya.risk.manager import RiskDecision, RiskManager
from maya.signals.models import SignalObject
from maya.signals.processor import SignalProcessor
from maya.strategy.engine import StrategyEngine
from maya.strategy.models import TradeIntent

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
# Fewer closed candles than this and the rolling context (ATR, swings,
# volume profile) is too thin to trust a setup.
MIN_HISTORY = 50

# Hyperliquid prefixes some low-priced coins with "k" (thousands of units,
# e.g. kPEPE). Binance lists the 1000x contract as "1000PEPEUSDT".
_K_PREFIX = "k"


@dataclass(frozen=True)
class Market:
    """One tradable perp, as Hyperliquid reports it."""

    coin: str                 # Hyperliquid name, e.g. "BTC", "kPEPE"
    volume_24h_usd: float
    max_leverage: int
    funding_rate: float       # hourly, signed
    mark_price: float

    @property
    def binance_symbol(self) -> str:
        if self.coin.startswith(_K_PREFIX) and self.coin[1:].isupper():
            return f"1000{self.coin[1:]}USDT"
        return f"{self.coin}USDT"


@dataclass
class Candidate:
    """A scanned coin and what each stage made of it."""

    market: Market
    intent: TradeIntent | None = None
    desk_verdict: str = "neutral"
    desk_rationale: str = ""
    decision: RiskDecision | None = None
    net_rr: float = 0.0
    fee_share: float = 0.0
    breakeven_win_rate: float = 1.0
    rejected: str = ""        # why it is not tradable, if it isn't
    patterns: list[str] = field(default_factory=list)
    signal: SignalObject | None = None   # the bar the setup fired on

    @property
    def tradable(self) -> bool:
        return not self.rejected and self.decision is not None and self.decision.approved


# -- free data sources ------------------------------------------------------ #
def fetch_hyperliquid_markets(timeout: float = 15.0) -> list[Market]:
    """Every main-DEX Hyperliquid perp with its live context. Public, free."""
    req = urllib.request.Request(
        HYPERLIQUID_INFO_URL, data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - fixed https URL
        meta, ctxs = json.loads(resp.read())
    return parse_hyperliquid_markets(meta, ctxs)


def parse_hyperliquid_markets(meta: dict, ctxs: list[dict]) -> list[Market]:
    markets = []
    for asset, ctx in zip(meta.get("universe", []), ctxs, strict=False):
        if asset.get("isDelisted"):
            continue
        try:
            markets.append(Market(
                coin=str(asset["name"]),
                volume_24h_usd=float(ctx.get("dayNtlVlm") or 0.0),
                max_leverage=int(asset.get("maxLeverage") or 0),
                funding_rate=float(ctx.get("funding") or 0.0),
                mark_price=float(ctx.get("markPx") or 0.0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return markets


def binance_candle_fetcher(settings: Settings) -> Callable[[str, int], list[Candle]]:
    """Recent closed Binance USD-M candles with real taker-buy volume."""
    import ccxt

    from maya.backtest.data_loader import _fetch_raw_klines, historical_candles
    from maya.timeframe import timeframe_ms

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
    tf_ms = timeframe_ms(settings.timeframe)

    def fetch(symbol: str, count: int) -> list[Candle]:
        now = ex.milliseconds()
        since = now - (count + 2) * tf_ms
        rows = _fetch_raw_klines(ex, settings.model_copy(update={"symbol": symbol}), since)
        # Drop the bar still forming: a scan must read closed candles only.
        rows = [r for r in rows if int(r[0]) + tf_ms <= now]
        return list(historical_candles(rows[-count:]))

    return fetch


# -- the scanner ------------------------------------------------------------- #
class Scanner:
    def __init__(self, settings: Settings, risk: RiskManager | None = None,
                 fetch_markets: Callable[[], list[Market]] | None = None,
                 fetch_candles: Callable[[str, int], list[Candle]] | None = None) -> None:
        self.settings = settings
        # Per-coin strategy runs never call the LLM: one review per coin per
        # candle would be slow and costly. The trade actually chosen still
        # gets the LLM veto (see review_best).
        self._coin_settings = settings.model_copy(update={"llm_enabled": False})
        self.risk = risk or RiskManager(settings, settings.start_equity)
        self.fees = FeeModel.for_settings(settings)
        self.fetch_markets = fetch_markets or fetch_hyperliquid_markets
        self.fetch_candles = fetch_candles or binance_candle_fetcher(settings)
        self._llm = StrategyEngine(settings).llm

    def universe(self) -> list[Market]:
        """Liquid coins the configured leverage can trade, busiest first.

        ``MAYA_SCAN_SYMBOLS`` pins an explicit list and skips the volume
        filter — useful when the Hyperliquid endpoint is unreachable."""
        explicit = [s.strip().upper() for s in self.settings.scan_symbols.split(",") if s.strip()]
        if explicit:
            return [Market(coin=c.removesuffix("USDT"), volume_24h_usd=0.0,
                           max_leverage=self.settings.leverage, funding_rate=0.0,
                           mark_price=0.0) for c in explicit]
        markets = [m for m in self.fetch_markets()
                   if m.volume_24h_usd >= self.settings.scan_min_volume_usd
                   and m.max_leverage >= self.settings.leverage]
        markets.sort(key=lambda m: m.volume_24h_usd, reverse=True)
        return markets[: self.settings.scan_max_coins]

    def evaluate(self, market: Market, now_ts: int) -> Candidate:
        cand = Candidate(market=market)
        try:
            candles = self.fetch_candles(market.binance_symbol, self.settings.scan_lookback)
        except Exception as exc:  # noqa: BLE001 - one bad coin must not stop the scan
            cand.rejected = f"no candles ({exc})"
            return cand
        if len(candles) < MIN_HISTORY:
            cand.rejected = f"only {len(candles)} candles"
            return cand

        processor = SignalProcessor(market.binance_symbol,
                                    htf_timeframes=self.settings.htf_list)
        strategy = StrategyEngine(self._coin_settings)
        intent, sig = None, None
        # Replay history so rolling state (CVD, swings, HTF) is warm; only an
        # intent on the final, just-closed bar is a live setup. The strategy
        # logs every intent it sees, which during a replay is hundreds of
        # stale setups per coin, so its logging is muted until the last bar.
        logger.disable("maya.strategy")
        try:
            for i, candle in enumerate(candles):
                sig = processor.process(candle, funding_rate=market.funding_rate)
                if i == len(candles) - 1:
                    logger.enable("maya.strategy")
                intent = strategy.evaluate(sig)
        finally:
            logger.enable("maya.strategy")
        cand.patterns = list(sig.patterns) if sig else []
        cand.signal = sig
        if intent is None:
            cand.rejected = "no setup on the last closed candle"
            return cand
        cand.intent = intent

        conf = desk.confer(candles[-200:], sig)
        cand.desk_verdict, cand.desk_rationale = conf.verdict, conf.rationale
        opposed = ((intent.direction == "long" and conf.verdict == BEARISH)
                   or (intent.direction == "short" and conf.verdict == BULLISH))
        if opposed and self.settings.scan_desk_veto:
            cand.rejected = f"chart desk disagrees ({conf.rationale})"
            return cand

        econ = economics(intent.entry_price, intent.stop_loss, intent.take_profit_1,
                         self.fees, self.risk.order_type)
        cand.net_rr = econ.net_rr
        cand.fee_share = econ.fee_share_of_target
        cand.breakeven_win_rate = econ.breakeven_win_rate

        cand.decision = self.risk.screen(intent, now_ts)
        if not cand.decision.approved:
            cand.rejected = cand.decision.reason
        return cand

    def scan(self, now_ts: int) -> list[Candidate]:
        """Every coin in the universe, tradable candidates first, best first."""
        try:
            markets = self.universe()
        except Exception as exc:  # noqa: BLE001
            logger.error("could not load the market universe: {}", exc)
            return []
        results = [self.evaluate(m, now_ts) for m in markets]
        results.sort(key=lambda c: (c.tradable, c.net_rr,
                                    c.intent.confluence_score if c.intent else 0),
                     reverse=True)
        return results

    def review_best(self, results: list[Candidate]) -> Candidate | None:
        """The best tradable candidate that also survives the LLM veto."""
        for cand in results:
            if not cand.tradable:
                break
            approved, reason = self._llm.review(cand.intent, cand.signal)
            if approved:
                return cand
            logger.info("LLM vetoed {} {}: {}", cand.market.coin,
                        cand.intent.direction, reason)
        return None


def format_table(results: list[Candidate]) -> str:
    lines = [f"{'coin':<8} {'setup':<26} {'dir':<5} {'net R:R':>7} {'fees/tgt':>8} "
             f"{'BE win%':>7} {'desk':<8} status"]
    for c in results:
        setup = c.intent.setup_name if c.intent else "-"
        direction = c.intent.direction if c.intent else "-"
        status = "TRADE" if c.tradable else c.rejected
        lines.append(
            f"{c.market.coin:<8} {setup:<26} {direction:<5} "
            f"{c.net_rr:>7.2f} {c.fee_share:>8.0%} {c.breakeven_win_rate:>7.0%} "
            f"{c.desk_verdict:<8} {status}" if c.intent else
            f"{c.market.coin:<8} {'-':<26} {'-':<5} {'':>7} {'':>8} {'':>7} {'':<8} {status}")
    return "\n".join(lines)


def main() -> None:
    import time

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    settings = get_settings()
    if not settings.uses_real_data:
        logger.critical("The scanner reads live market data — use MAYA_MODE=paper (or "
                        "testnet/live), not sim.")
        sys.exit(2)
    scanner = Scanner(settings)
    results = scanner.scan(int(time.time() * 1000))
    print(format_table(results))
    tradable = [c for c in results if c.tradable]
    logger.info("{} coins scanned, {} tradable after the fee gate", len(results), len(tradable))


if __name__ == "__main__":
    main()
