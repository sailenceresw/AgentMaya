"""Hypothesis registry: every test leaves a row, pass, fail or crash.

The table lives in the same SQLite file as the trade journal
(``maya_journal.db`` by default) so the record of what was tried sits next to
the record of what was traded.

The registry is what makes the multiple-testing correction honest. The
threshold is computed from its row count, so deleting rows to get a softer
threshold is the one thing this module is built to discourage. There is no
delete method on purpose.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    row          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL,
    description  TEXT,
    tested_at    INTEGER NOT NULL,
    t_stat       REAL,
    passed_gate  INTEGER NOT NULL,
    reason       TEXT,
    kind         TEXT NOT NULL DEFAULT 'research',
    lesson       TEXT
);
CREATE TABLE IF NOT EXISTS holdout_runs (
    id      TEXT PRIMARY KEY,
    run_at  INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class RegistryRow:
    id: str
    description: str
    tested_at: int
    t_stat: float | None
    passed_gate: bool
    reason: str
    kind: str
    lesson: str = ""


def make_lesson(hypothesis_id: str, reason: str, kind: str = "research") -> str:
    """One short line saying a hypothesis is blocked and why.

    Built from the gate's own failure text, shortened to its first clause, so
    the lesson can never say something the test did not find.
    """
    first = (reason or "no reason recorded").split(";")[0].strip()
    where = "on the holdout" if kind == "holdout" else "in research"
    return f"{hypothesis_id} blocked {where}: {first}"


class HoldoutAlreadyUsed(RuntimeError):
    """The holdout for this hypothesis was already spent."""


class HypothesisRegistry:
    def __init__(self, db_path: str = "maya_journal.db") -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            # Databases created before the lesson column existed.
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(hypotheses)")}
            if "lesson" not in cols:
                self.conn.execute("ALTER TABLE hypotheses ADD COLUMN lesson TEXT")
            self.conn.commit()

    def record(self, hypothesis_id: str, description: str, t_stat: float | None,
               passed_gate: bool, reason: str, kind: str = "research",
               tested_at: int | None = None) -> None:
        lesson = "" if passed_gate else make_lesson(hypothesis_id, reason, kind)
        with self._lock:
            self.conn.execute(
                "INSERT INTO hypotheses (id, description, tested_at, t_stat, passed_gate, "
                "reason, kind, lesson) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hypothesis_id, description,
                 int(time.time() * 1000) if tested_at is None else tested_at,
                 t_stat, int(passed_gate), reason, kind, lesson))
            self.conn.commit()

    def count_tests(self) -> int:
        """Research tests run so far. Holdout rows are confirmations of a test
        already counted, so they do not raise the threshold a second time."""
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE kind = 'research'").fetchone()[0]

    def rows(self) -> list[RegistryRow]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, description, tested_at, t_stat, passed_gate, reason, kind, "
                "lesson FROM hypotheses ORDER BY row")
            return [RegistryRow(r[0], r[1] or "", r[2], r[3], bool(r[4]), r[5] or "", r[6],
                                r[7] or "")
                    for r in cur.fetchall()]

    def failures(self) -> list[RegistryRow]:
        """Every failed test, oldest first. Read this before proposing a new
        hypothesis: an idea already blocked should not come back renamed."""
        return [r for r in self.rows() if not r.passed_gate]

    def research_rows_for(self, hypothesis_id: str) -> list[RegistryRow]:
        return [r for r in self.rows() if r.id == hypothesis_id and r.kind == "research"]

    def has_research_pass(self, hypothesis_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM hypotheses WHERE id = ? AND kind = 'research' "
                "AND passed_gate = 1 LIMIT 1", (hypothesis_id,)).fetchone()
        return row is not None

    def claim_holdout(self, hypothesis_id: str) -> None:
        """Spend the holdout for ``hypothesis_id``, or raise if already spent.

        The claim is written before the holdout is evaluated, so a run that
        crashes halfway still counts as used. Otherwise "it crashed, run it
        again" would be a way to look at the holdout twice.
        """
        with self._lock:
            try:
                self.conn.execute("INSERT INTO holdout_runs (id, run_at) VALUES (?, ?)",
                                  (hypothesis_id, int(time.time() * 1000)))
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                raise HoldoutAlreadyUsed(
                    f"holdout for {hypothesis_id!r} was already run; it runs once") from exc

    def holdout_used(self, hypothesis_id: str) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM holdout_runs WHERE id = ?",
                                    (hypothesis_id,)).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self.conn.close()
