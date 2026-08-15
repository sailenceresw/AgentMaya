"""Maya — the desk. Runs the conference and makes the call.

Every analyst reads the same bar and files a :class:`Finding`. Maya combines
them into a :class:`Conference`.

Three rules, each there for a reason:

1. **Sentinel's BLOCK is absolute.** No weight of bullish opinion overrides
   "the account cannot take another trade".
2. **Disagreement suppresses, it does not average.** Two analysts pointing
   opposite ways is not a weak signal in the middle — it is a reason not to
   act. Averaging would turn a genuine conflict into a small position, which
   is how a system ends up trading its own confusion.
3. **Weights are fixed and declared, not fitted.** They encode how much each
   analyst has *earned* trust, and nothing here has earned much: over 24 months
   no factor cleared |t| > 2. Fitting weights to the same window that produced
   the measurements would be curve-fitting with extra steps.
"""
from __future__ import annotations

from maya.agents import atlas, echo, flow, sentinel
from maya.agents.models import BEARISH, BLOCK, BULLISH, NEUTRAL, Conference, Finding
from maya.data.models import Candle
from maya.risk.manager import RiskManager
from maya.signals.models import SignalObject

AGENT = "Maya"

# How much each analyst's opinion counts. Atlas leads because levels are the
# only input derived purely from price structure rather than from a signal
# whose predictive power measured at zero.
WEIGHTS = {
    atlas.AGENT: 1.0,
    echo.AGENT: 0.7,
    flow.AGENT: 0.6,
}
# Net conviction required before Maya says anything other than neutral.
MIN_CONVICTION = 0.35
# If the losing side holds more than this share of the total opinion, the desk
# is split and Maya stands down regardless of net score.
MAX_DISSENT = 0.40


def confer(candles: list[Candle], sig: SignalObject,
           risk: RiskManager | None = None, now_ts: int | None = None) -> Conference:
    """Hold the conference for one bar and return the desk's view."""
    levels = atlas.find_levels(candles)
    findings: list[Finding] = [
        atlas.observe(candles, levels),
        echo.observe(candles, levels),
        flow.observe(sig),
    ]

    if risk is not None:
        ts = now_ts if now_ts is not None else sig.timestamp
        guard = sentinel.observe(risk, ts)
        findings.append(guard)
        if guard.verdict == BLOCK:
            return Conference(NEUTRAL, 0.0, findings,
                              blocked_by=guard.agent, rationale=guard.evidence)

    bull = sum(f.confidence * WEIGHTS.get(f.agent, 0.0)
               for f in findings if f.verdict == BULLISH)
    bear = sum(f.confidence * WEIGHTS.get(f.agent, 0.0)
               for f in findings if f.verdict == BEARISH)
    total = bull + bear
    net = bull - bear

    if total <= 0:
        return Conference(NEUTRAL, 0.0, findings,
                          rationale="no analyst had an opinion")

    dissent = min(bull, bear) / total
    if dissent > MAX_DISSENT:
        return Conference(
            NEUTRAL, 0.0, findings,
            rationale=f"desk split ({bull:.2f} bull vs {bear:.2f} bear) — "
                      f"conflict is a reason to stand down, not to size small")

    if abs(net) < MIN_CONVICTION:
        return Conference(NEUTRAL, 0.0, findings,
                          rationale=f"net conviction {abs(net):.2f} below "
                                    f"{MIN_CONVICTION} threshold")

    verdict = BULLISH if net > 0 else BEARISH
    speakers = ", ".join(f"{f.agent} {f.verdict}" for f in findings
                         if f.verdict in (BULLISH, BEARISH))
    return Conference(verdict, round(abs(net), 4), findings,
                      rationale=f"{speakers} -> {verdict} ({abs(net):.2f})")
