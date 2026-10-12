"""Layer 3 orchestrator.

Builds rolling context, runs the three setups, scores confluence, applies the
optional LLM veto, and emits at most one :class:`TradeIntent` per candle. The
engine NEVER places orders — it only produces intents for Layer 4 to screen.
"""
from __future__ import annotations

from collections import deque

from loguru import logger

from maya.config import RiskLimits, Settings
from maya.signals import technicals as ta
from maya.signals.models import SignalObject
from maya.strategy import setups as setup_mod
from maya.strategy.confluence import score_confluence
from maya.strategy.llm_filter import LLMFilter
from maya.strategy.models import TradeIntent
from maya.strategy.setups import SetupContext


class StrategyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.highs: deque[float] = deque(maxlen=15)
        self.lows: deque[float] = deque(maxlen=15)
        self.closes: deque[float] = deque(maxlen=30)
        self.llm = LLMFilter(settings)
        self._setups = tuple(
            s for s in setup_mod.ALL_SETUPS
            if s is not setup_mod.compression_breakout or settings.compression_breakout_enabled)

    def observe(self, sig: SignalObject) -> SetupContext:
        """Record this bar in the rolling swing context and return the context
        as it stood *before* the bar (so a sweep compares against prior swings).

        Must run on every candle, position or not. The graph used to skip the
        strategy while a trade was open, so these windows froze for the whole
        hold and the first setups after the close were judged against swings
        from before the entry.
        """
        return self._context(sig)

    def _context(self, sig: SignalObject) -> SetupContext:
        prev_high = max(self.highs) if self.highs else sig.high
        prev_low = min(self.lows) if self.lows else sig.low
        self.highs.append(sig.high)
        self.lows.append(sig.low)
        self.closes.append(sig.price)

        # A sweep = wick beyond the prior swing but close back inside (spec 6.2).
        swept_high = sig.high > prev_high and sig.price < prev_high
        swept_low = sig.low < prev_low and sig.price > prev_low
        # A breakout = close decisively beyond the prior swing.
        breakout_up = sig.price > prev_high
        breakout_down = sig.price < prev_low

        return SetupContext(
            swing_high_15m=prev_high,
            swing_low_15m=prev_low,
            range_pct=sig.range_pct,
            swept_high=swept_high,
            swept_low=swept_low,
            breakout_up=breakout_up,
            breakout_down=breakout_down,
        )

    def evaluate(self, sig: SignalObject, ctx: SetupContext | None = None) -> TradeIntent | None:
        """Best intent on this bar, or None. Pass the ``ctx`` returned by
        :meth:`observe` for this bar; without one, the bar is observed here."""
        if ctx is None:
            ctx = self.observe(sig)

        candidates: list[TradeIntent] = []
        for setup in self._setups:
            intent = setup(sig, ctx)
            if intent is not None:
                candidates.append(intent)

        best: TradeIntent | None = None
        for intent in candidates:
            # Passed by keyword: score_confluence takes several optional
            # factors after `sig`, and calling it positionally silently left
            # htf_alignment stuck at its default of False, capping the
            # achievable score below MIN_CONFLUENCE_SCORE.
            score, factors = score_confluence(
                intent, sig,
                htf_aligned=ta.aligned(sig.htf, intent.direction),
                liquidation_target=ta.targets_liquidity(
                    sig.htf, intent.direction,
                    intent.entry_price, intent.take_profit_1),
            )
            intent.confluence_score = score
            intent.confluences = factors
            if score < RiskLimits.MIN_CONFLUENCE_SCORE:
                logger.debug("setup {} below confluence ({} < {})",
                             intent.setup_name, score, RiskLimits.MIN_CONFLUENCE_SCORE)
                continue
            if best is None or score > best.confluence_score:
                best = intent

        if best is None:
            return None

        approved, reason = self.llm.review(best, sig)
        if not approved:
            logger.info("LLM vetoed {} trade: {}", best.setup_name, reason)
            return None

        logger.info("Trade intent: {} {} @ {:.2f} score={} ({})",
                    best.direction, best.setup_name, best.entry_price,
                    best.confluence_score, ",".join(best.confluences))
        return best
