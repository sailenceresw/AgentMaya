"""Confluence scoring system (spec section 6.3)."""
from __future__ import annotations

from maya.signals.models import SignalObject
from maya.strategy.models import TradeIntent


def score_confluence(intent: TradeIntent, sig: SignalObject,
                     htf_aligned: bool = False, news_clear: bool = True,
                     in_session: bool = True,
                     liquidation_target: bool = False) -> tuple[int, list[str]]:
    """Return ``(score, factors)``. Trade only if score >= MIN_CONFLUENCE_SCORE."""
    score = 0
    factors: list[str] = []

    # +2 clean setup pattern match (a candidate already implies this)
    score += 2
    factors.append("setup_match")

    if htf_aligned:
        score += 1
        factors.append("htf_alignment")

    if sig.volume > 1.5 * sig.avg_volume:
        score += 1
        factors.append("volume")

    cvd_dir = "bullish" if intent.direction == "long" else "bearish"
    if sig.cvd_divergence == cvd_dir or (sig.cvd_1m > 0) == (intent.direction == "long"):
        score += 1
        factors.append("cvd")

    funding_aligns = (
        (intent.direction == "short" and sig.funding_rate > 0)
        or (intent.direction == "long" and sig.funding_rate < 0)
    )
    if funding_aligns:
        score += 1
        factors.append("funding")

    if liquidation_target:
        score += 1
        factors.append("liquidation_cluster")

    if news_clear:
        score += 1
        factors.append("news_clear")

    if in_session:
        score += 1
        factors.append("session")

    if sig.amt_phase == "expansion":
        score += 1
        factors.append("amt_expansion")

    return score, factors
