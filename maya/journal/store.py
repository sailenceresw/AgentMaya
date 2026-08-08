"""Layer 6 — Trade journal (SQLite) and performance metrics.

Records every closed trade with full context (spec section 9.1) and computes
summary metrics (spec section 9.2). SQLite keeps the MVP dependency-free; the
schema maps cleanly onto the PostgreSQL table in the spec.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict

from loguru import logger

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
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def record(self, result: TradeResult, confluences: list[str]) -> None:
        pos = result.position
        pnl_pct = result.pnl_usdt / pos.notional * 100 if pos.notional else 0.0
        self.conn.execute(
            "INSERT INTO trades VALUES "
            "(:id,:te,:tx,:sym,:dir,:setup,:ep,:xp,:sl,:tp,:size,:lev,"
            ":pnl,:pnlpct,:fees,:rrr,:rrp,:res,:cs,:conf,:reason)",
            {
                "id": str(uuid.uuid4()),
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

    def metrics(self) -> dict:
        """Aggregate performance metrics (spec section 9.2)."""
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

    def close(self) -> None:
        self.conn.close()
