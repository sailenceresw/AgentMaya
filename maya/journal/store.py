"""Layer 6 — Trade journal (SQLite) and performance metrics.

Records every closed trade with full context (spec section 9.1) and computes
summary metrics (spec section 9.2). SQLite keeps the MVP dependency-free; the
schema maps cleanly onto the PostgreSQL table in the spec.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid


from maya.execution.engine import TradeResult

_DAY_MS = 86_400_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id               TEXT PRIMARY KEY,
    timestamp_entry  INTEGER,
    timestamp_exit   INTEGER,
    symbol           TEXT,
    direction        TEXT,
    setup_name       TEXT,
    entry_price      REAL,
    exit_price       REAL,
    stop_loss        REAL,
    take_profit      REAL,
    position_size    REAL,
    leverage         INTEGER,
    pnl_usdt         REAL,
    pnl_pct          REAL,
    fees_usdt        REAL,
    rr_realized      REAL,
    rr_planned       REAL,
    result           TEXT,
    confluence_score INTEGER,
    confluences      TEXT,
    exit_reason      TEXT
);
"""


def _utc_day_start_ms(now_ts: int) -> int:
    """Start of the current UTC day, in epoch ms, for ``now_ts`` (epoch ms)."""
    return (now_ts // _DAY_MS) * _DAY_MS


class TradeJournal:
    def __init__(self, db_path: str = "maya_journal.db") -> None:
        # The journal is written from more than one thread: the candle-driven
        # graph runs on the main thread, PositionMonitor writes exchange-fill
        # closes from its poll thread, and the Layer 7 API reads from its
        # handler threads. sqlite3 rejects cross-thread use by default, which
        # made every monitor-detected close raise ProgrammingError inside the
        # monitor's catch-all — the trade went unjournaled *and*
        # risk.on_trade_closed never ran, so consecutive_losses, daily PnL and
        # the kill switch silently stopped updating on testnet/live.
        #
        # check_same_thread=False lifts the restriction; the lock below is
        # what actually makes concurrent use safe.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        # Ids written by *this* process. The journal is deliberately durable
        # across restarts (it backs the spec-8.4 risk-state restoration), so
        # "how did this run do" cannot be answered by querying the whole
        # table. Timestamps can't answer it either: the sim and scenario feeds
        # emit synthetic clocks (the scripted setup is stamped in 2023), so
        # filtering on timestamp_exit >= process-start would drop the very
        # trades the run just made.
        self._session_ids: list[str] = []

    def record(self, result: TradeResult, confluences: list[str]) -> None:
        pos = result.position
        pnl_pct = result.pnl_usdt / pos.notional * 100 if pos.notional else 0.0
        trade_id = str(uuid.uuid4())
        self._session_ids.append(trade_id)
        with self._lock:
            self.conn.execute(
            "INSERT INTO trades VALUES "
            "(:id,:te,:tx,:sym,:dir,:setup,:ep,:xp,:sl,:tp,:size,:lev,"
            ":pnl,:pnlpct,:fees,:rrr,:rrp,:res,:cs,:conf,:reason)",
            {
                "id": trade_id,
                "te": pos.opened_ts, "tx": result.exit_ts,
                "sym": pos.symbol, "dir": pos.direction, "setup": pos.setup_name,
                "ep": pos.entry_price, "xp": result.exit_price,
                "sl": pos.stop_loss, "tp": pos.take_profit,
                "size": pos.quantity, "lev": pos.leverage,
                "pnl": result.pnl_usdt, "pnlpct": pnl_pct, "fees": result.fees_usdt,
                "rrr": result.rr_realized, "rrp": pos.rr_planned,
                "res": result.result, "cs": pos.confluence_score,
                "conf": json.dumps(confluences), "reason": result.reason,
            },
            )
            self.conn.commit()

    # -- state reconciliation (spec section 8.4) ---------------------------- #
    # These back the risk-floor counters (trades_today, consecutive_losses,
    # daily_pnl_pct) that AccountState previously always started at zero. On
    # a fresh restart they let the risk manager restore its real session
    # state from what actually happened, instead of amnesia'ing back to a
    # clean (more permissive) slate.
    def trades_since(self, since_ts: int) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM trades WHERE timestamp_exit >= ? ORDER BY timestamp_exit ASC",
                (since_ts,),
            ).fetchall()

    def trades_today(self, now_ts: int | None = None) -> int:
        now_ts = now_ts if now_ts is not None else int(time.time() * 1000)
        return len(self.trades_since(_utc_day_start_ms(now_ts)))

    def pnl_today_usdt(self, now_ts: int | None = None) -> float:
        now_ts = now_ts if now_ts is not None else int(time.time() * 1000)
        rows = self.trades_since(_utc_day_start_ms(now_ts))
        return sum(r["pnl_usdt"] for r in rows)

    def consecutive_losses(self) -> int:
        """Losing streak walking back from the most recently closed trade,
        all-time (a streak doesn't reset just because midnight UTC passed)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT result FROM trades ORDER BY timestamp_exit DESC"
            ).fetchall()
        streak = 0
        for r in rows:
            if r["result"] == "loss":
                streak += 1
            else:
                break
        return streak

    def session_state(self, now_ts: int | None = None) -> dict:
        """Everything the risk manager needs to restore state on restart."""
        now_ts = now_ts if now_ts is not None else int(time.time() * 1000)
        return {
            "trades_today": self.trades_today(now_ts),
            "consecutive_losses": self.consecutive_losses(),
            "pnl_today_usdt": self.pnl_today_usdt(now_ts),
        }

    def metrics(self, session_only: bool = False) -> dict:
        """Aggregate performance metrics (spec section 9.2).

        ``session_only=True`` restricts the aggregate to trades this process
        recorded. Use it for anything reported as "what this run did" — the
        all-time figures mix sim, testnet and live runs together, which would
        quietly corrupt the §14.1 profit-factor / win-rate gates.
        """
        if session_only:
            if not self._session_ids:
                return {"trades": 0}
            placeholders = ",".join("?" * len(self._session_ids))
            with self._lock:
                rows = self.conn.execute(
                    f"SELECT * FROM trades WHERE id IN ({placeholders})",
                    self._session_ids,
                ).fetchall()
        else:
            with self._lock:
                rows = self.conn.execute("SELECT * FROM trades").fetchall()
        n = len(rows)
        if n == 0:
            return {"trades": 0}
        wins = [r for r in rows if r["result"] == "win"]
        losses = [r for r in rows if r["result"] == "loss"]
        gross_profit = sum(r["pnl_usdt"] for r in wins)
        gross_loss = -sum(r["pnl_usdt"] for r in losses)
        total_pnl = sum(r["pnl_usdt"] for r in rows)
        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        return {
            "trades": n,
            "win_rate": round(len(wins) / n * 100, 2),
            "total_pnl_usdt": round(total_pnl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else float("inf"),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(total_pnl / n, 2),
            "total_fees": round(sum(r["fees_usdt"] for r in rows), 2),
        }

    def recent(self, limit: int = 20) -> list[dict]:
        """Most recently closed trades, newest first — for the Layer 7 API."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT setup_name, direction, pnl_usdt, result, exit_reason, "
                "timestamp_exit FROM trades ORDER BY timestamp_exit DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
