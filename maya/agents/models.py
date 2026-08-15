"""What one agent says, and what the desk concludes.

Every specialist speaks the same way: a verdict, how strongly it believes it,
and the evidence behind it. Uniform shape is what makes the conversation
inspectable — when Maya declines a trade you can read exactly who disagreed
and why, instead of watching a single confluence integer go from 7 to 6.

Confidence is deliberately not a probability. Nothing here is calibrated
against outcomes, and dressing a heuristic in probabilistic clothing invites
arithmetic it cannot support. Treat it as a strength of opinion in [0, 1].
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Verdicts every agent draws from, so the coordinator never has to guess at
# an agent-specific vocabulary.
BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"
BLOCK = "block"      # Sentinel only: a hard veto, not an opinion


@dataclass
class Finding:
    """One agent's read of the current bar."""

    agent: str
    verdict: str                       # bullish / bearish / neutral / block
    confidence: float = 0.0            # 0..1, strength of opinion
    evidence: str = ""                 # human-readable, shows up in logs
    data: dict = field(default_factory=dict)   # structured detail

    @property
    def signed(self) -> float:
        """Confidence as a direction: + for bullish, - for bearish, 0 else."""
        if self.verdict == BULLISH:
            return self.confidence
        if self.verdict == BEARISH:
            return -self.confidence
        return 0.0


@dataclass
class Conference:
    """The desk's combined view after everyone has spoken."""

    verdict: str = NEUTRAL
    confidence: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    blocked_by: str | None = None       # agent name if vetoed
    rationale: str = ""

    @property
    def agrees_with(self) -> str | None:
        """Direction the desk supports, or None if neutral/blocked."""
        if self.blocked_by or self.verdict == NEUTRAL:
            return None
        return "long" if self.verdict == BULLISH else "short"

    def supports(self, direction: str) -> bool:
        return self.agrees_with == direction

    def by_agent(self, name: str) -> Finding | None:
        for f in self.findings:
            if f.agent == name:
                return f
        return None

    def summary(self) -> str:
        """One line per agent — what the desk said, for the journal and logs."""
        parts = [f"{f.agent}:{f.verdict}({f.confidence:.2f})" for f in self.findings]
        head = f"{self.verdict}({self.confidence:+.2f})"
        if self.blocked_by:
            head = f"BLOCKED by {self.blocked_by}"
        return f"{head} | " + " ".join(parts)
