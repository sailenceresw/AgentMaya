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
from collections import deque
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
from maya.timeframe import timeframe_ms

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
        sym_settings = settings.model_copy(update={"symbol": symbol})
        # One request returns at most one page, counted forward from ``since``.
        # Regression: asking for more than a page returned the OLDEST page,
        # so a long lookback would have read hours-old candles as "the last
        # closed candle". Page forward until the present.
        rows: list[list] = []
        start = since
        while True:
            page = _fetch_raw_klines(ex, sym_settings, start)
            rows += [r for r in page if not rows or int(r[0]) > int(rows[-1][0])]
            if not page or int(page[-1][0]) + 2 * tf_ms > now or len(rows) > count + 2:
                break
            start = int(page[-1][0]) + tf_ms
        # Drop the bar still forming: a scan must read closed candles only.
        rows = [r for r in rows if int(r[0]) + tf_ms <= now]
        return list(historical_candles(rows[-count:]))

    return fetch


@dataclass
class _CoinState:
    """One coin's readings, kept alive between scans."""

    processor: SignalProcessor
    strategy: StrategyEngine
    candles: deque          # recent closed candles, for the desk and the history floor
    last_ts: int = 0        # newest candle processed
    intent: TradeIntent | None = None
    signal: SignalObject | None = None


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
        self._state: dict[str, _CoinState] = {}

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

    def _advance(self, market: Market, now_ts: int) -> _CoinState:
        """Bring one coin's readings up to its newest closed candle.

        Regression: every scan rebuilt each coin from scratch out of the last
        ``scan_lookback`` candles. Readings that carry state from further back
        then differed from the continuous run the strategy was measured on:
        replaying 10 hours of 15 coins, the rebuilt scan missed 2 of 9 setups.
        Now each coin is warmed once and fed only its new candles afterwards,
        exactly as the backtest and the single-coin agent run. A gap the
        catch-up fetch cannot bridge (a coin off the list for a while, a long
        outage) rebuilds that coin, because a hole in the history would be
        worse than a fresh warm-up.
        """
        sym, tf_ms = market.binance_symbol, timeframe_ms(self.settings.timeframe)
        st = self._state.get(sym)
        if st is not None:
            behind = max(0, (now_ts - st.last_ts) // tf_ms)
            fresh = self.fetch_candles(sym, int(min(self.settings.scan_lookback, behind + 5)))
            new = [c for c in fresh if c.timestamp > st.last_ts]
            if new and new[0].timestamp > st.last_ts + tf_ms:
                st = None                       # a hole: rebuild below
            else:
                self._feed(st, new, market)
                return st
        candles = self.fetch_candles(sym, self.settings.scan_lookback)
        st = _CoinState(SignalProcessor(sym, htf_timeframes=self.settings.htf_list),
                        StrategyEngine(self._coin_settings),
                        deque(maxlen=max(self.settings.scan_lookback, 200)))
        self._feed(st, candles, market)
        self._state[sym] = st
        return st

    @staticmethod
    def _feed(st: _CoinState, candles: list[Candle], market: Market) -> None:
        """Process candles in order; the intent kept is the last candle's.

        The strategy logs every intent it sees, which during a warm-up is
        hundreds of stale setups, so its logging is muted until the last bar.
        """
        logger.disable("maya.strategy")
        try:
            for i, candle in enumerate(candles):
                if i == len(candles) - 1:
                    logger.enable("maya.strategy")
                st.signal = st.processor.process(candle, funding_rate=market.funding_rate)
                st.intent = st.strategy.evaluate(st.signal)
                st.candles.append(candle)
                st.last_ts = candle.timestamp
        finally:
            logger.enable("maya.strategy")

    def evaluate(self, market: Market, now_ts: int) -> Candidate:
        cand = Candidate(market=market)
        try:
            st = self._advance(market, now_ts)
        except Exception as exc:  # noqa: BLE001 - one bad coin must not stop the scan
            self._state.pop(market.binance_symbol, None)
            cand.rejected = f"no candles ({exc})"
            return cand
        if len(st.candles) < MIN_HISTORY:
            cand.rejected = f"only {len(st.candles)} candles"
            return cand
        # A setup counts only on the newest closed candle. If Binance has not
        # published the last bar yet, the reading is a candle old and must not
        # be acted on a second time.
        if st.last_ts + 2 * timeframe_ms(self.settings.timeframe) <= now_ts:
            cand.rejected = "no new closed candle yet"
            return cand
        candles, sig, intent = list(st.candles), st.signal, st.intent
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
        # Forget coins that left the universe; one that returns is rebuilt.
        keep = {m.binance_symbol for m in markets}
        for sym in [s for s in self._state if s not in keep]:
            del self._state[sym]
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
