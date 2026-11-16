"""Lessons memory: what each losing trade taught, and a veto built on it.

Two halves, both deterministic and both free of any LLM import:

- :func:`derive_lesson` turns a closed losing trade into a reason code and one
  plain sentence. The journal stores it in the ``lessons`` table.
- :class:`LessonsReviewer` looks at an entry about to be taken, finds past
  trades of the same setup and direction in a similar context, and vetoes the
  entry when that context has a losing record.

The reviewer sits before the risk layer and can only remove a trade. It never
approves one, never sizes one and never touches a limit, so a trade it lets
through still has to pass every hard check in ``maya.risk``.

This is a rule about the system's own history, not a measured edge. Whether it
improves results is a hypothesis for ``maya.research`` like any other.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# A stop-out inside this many minutes means the entry was wrong at once,
# which is a different mistake from a trade that worked for a while first.
FAST_STOP_MINUTES = 5


@dataclass(frozen=True)
class Lesson:
    reason_code: str
    sentence: str


def derive_lesson(*, setup_name: str, direction: str, confluences: list[str],
                  exit_reason: str, pnl_usdt: float, fees_usdt: float,
                  minutes_open: float) -> Lesson | None:
    """Reason code and sentence for a losing trade; None for anything else.

    The codes are ordered from most to least specific, and the first that
    fits is used, so each loss gets exactly one.
    """
    if pnl_usdt >= 0:
        return None
    gross = pnl_usdt + fees_usdt
    if gross > 0:
        return Lesson("fees_ate_profit",
                      f"{setup_name} {direction} made ${gross:.4f} before fees and lost "
                      f"${-pnl_usdt:.4f} after them; the move was too small for the cost.")
    if "htf_alignment" not in confluences:
        return Lesson("against_15m_trend",
                      f"{setup_name} {direction} was taken without higher-timeframe "
                      "alignment and stopped out.")
    if exit_reason == "max_duration":
        return Lesson("ran_out_of_time",
                      f"{setup_name} {direction} never reached target or stop and was "
                      "closed at the time limit for a loss.")
    if exit_reason == "stop_loss" and minutes_open <= FAST_STOP_MINUTES:
        return Lesson("stopped_fast",
                      f"{setup_name} {direction} was stopped within {minutes_open:.0f} "
                      "minutes; price never moved in its favour.")
    if exit_reason == "stop_loss":
        return Lesson("stopped_out",
                      f"{setup_name} {direction} was stopped out after "
                      f"{minutes_open:.0f} minutes.")
    return Lesson("closed_at_loss",
                  f"{setup_name} {direction} was closed at a loss ({exit_reason}).")


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    """Overlap of two factor sets, 0..1. Two empty sets are identical."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


@dataclass(frozen=True)
class Review:
    veto: bool
    reason: str
    matches: int = 0
    wins: int = 0


class LessonsReviewer:
    """Vetoes an entry whose context has a losing record in the journal."""

    def __init__(self, journal, similarity: float, min_matches: int,
                 min_win_rate: float) -> None:
        self.journal = journal
        self.similarity = similarity
        self.min_matches = min_matches
        self.min_win_rate = min_win_rate

    def review(self, setup_name: str, direction: str, confluences: list[str],
               now_ts: int) -> Review:
        # Only trades closed before this bar: in a backtest the journal is
        # filled as the run goes, and reading a later trade would be lookahead.
        rows = self.journal.closed_trades_for(setup_name, direction, before_ts=now_ts)
        similar = [r for r in rows
                   if jaccard(confluences, json.loads(r["confluences"] or "[]")) >= self.similarity]
        n = len(similar)
        wins = sum(1 for r in similar if r["result"] == "win")
        if n < self.min_matches:
            return Review(False, f"{n} similar past trades, fewer than {self.min_matches}", n, wins)
        rate = wins / n
        if rate < self.min_win_rate:
            codes = self.journal.lesson_codes_for(setup_name, direction, before_ts=now_ts)
            top = max(codes, key=codes.get) if codes else "no lesson recorded"
            return Review(
                True,
                f"lessons veto: {setup_name} {direction} in this context won {wins} of {n} "
                f"({rate:.0%} < {self.min_win_rate:.0%}); most common lesson: {top}",
                n, wins)
        return Review(False, f"{wins} of {n} similar past trades won", n, wins)
