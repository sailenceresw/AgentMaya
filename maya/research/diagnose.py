"""Diagnostics: *why* a pipeline hypothesis lost, on the research window only.

    python -m maya.research diagnose cvd_flip
    python -m maya.research diagnose baseline_1m_setups --symbols BTCUSDT

This is exploration, not a test, and it is fenced accordingly:

- It reads ``ResearchData.research`` only. The holdout is never loaded into
  the analysis, so a diagnosis cannot leak the holdout into the next idea.
- It writes **no registry row**. Looking does not raise the threshold, and
  passing nothing here passes anything.
- Every finding is a lead, not a result. A lead becomes a *new* hypothesis id
  with rules fixed before it runs, and goes through the harness like any
  other. Dozens of cells are printed here; at |t| = 3 roughly one in 370
  pure-noise cells still clears it, so the report counts the cells it showed.

What it measures per trade: how far price went for and against the trade
before it closed (MFE/MAE, in R and bps), what the same entry would have made
held for longer fixed horizons, and the context at entry (session, volatility,
higher-timeframe trend). Then it groups the trades by each of those.
"""
from __future__ import annotations

import bisect
import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

from maya.data.models import Candle
from maya.research.stats import mean, t_stat

HORIZONS = (1, 4, 16, 64)
# A cell is shown as a lead only with this many trades and this t on net (positive).
LEAD_MIN_N = 100
LEAD_MIN_T = 3.0
SESSIONS = ((0, 8, "asia 00-08"), (8, 13, "europe 08-13"), (13, 21, "us 13-21"),
            (21, 24, "late 21-24"))


@dataclass
class TradeDiag:
    symbol: str
    setup: str
    direction: str
    ts: int
    gross_bps: float
    net_bps: float
    risk_bps: float
    gross_r: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    session: str
    vol: float            # mean bar range / close over the 14 bars before entry
    with_trend: bool | None
    forward_bps: dict[int, float | None]


def _session(ts: int) -> str:
    h = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour
    return next(name for lo, hi, name in SESSIONS if lo <= h < hi)


def _entry_index(times: list[int], ts: int) -> int | None:
    i = bisect.bisect_right(times, ts) - 1
    return i if i >= 0 else None


def diagnose_trade(row: dict, candles: list[Candle], times: list[int],
                   round_trip_bps: float, trend_bars: int) -> TradeDiag | None:
    """Excursions, horizons and context for one journaled trade."""
    i = _entry_index(times, int(row["timestamp_entry"]))
    entry = row.get("entry_price") or 0.0
    stop = row.get("stop_loss") or 0.0
    if i is None or entry <= 0 or row.get("gross_return") is None:
        return None
    long = row["direction"] == "long"
    sign = 1.0 if long else -1.0
    # The journal stores the stop at *close*, after the profit lock or trail
    # may have moved it to breakeven, which made risk tiny and R explode
    # (first cvd_flip diagnosis: mean +4.3R on a losing book). The target never
    # moves and rr_planned = |TP - entry| / |entry - original stop|, so the
    # original risk is recovered from those two.
    tp, rrp = row.get("take_profit") or 0.0, row.get("rr_planned") or 0.0
    if tp and rrp > 0:
        risk = abs(tp - entry) / rrp / entry
    else:
        risk = abs(entry - stop) / entry if stop else 0.0
    j = _entry_index(times, int(row.get("timestamp_exit") or row["timestamp_entry"])) or i
    # The entry bar's own range happened before the fill (entry is at its
    # close), so excursions start at the next bar.
    path = candles[i + 1: max(j, i) + 1]
    if path:
        best = max(c.high for c in path) if long else min(c.low for c in path)
        worst = min(c.low for c in path) if long else max(c.high for c in path)
        mfe = sign * (best - entry) / entry
        mae = -sign * (worst - entry) / entry
    else:
        mfe = mae = 0.0
    fwd: dict[int, float | None] = {}
    base = candles[i].close
    for h in HORIZONS:
        fwd[h] = (sign * (candles[i + h].close / base - 1) * 1e4
                  if i + h < len(candles) and base > 0 else None)
    window = candles[max(0, i - 13): i + 1]
    vol = mean([(c.high - c.low) / c.close for c in window if c.close > 0])
    with_trend = None
    if i - trend_bars >= 0 and candles[i - trend_bars].close > 0:
        trend = candles[i].close - candles[i - trend_bars].close
        if trend != 0:
            with_trend = (trend > 0) == long
    g = row["gross_return"] * 1e4
    return TradeDiag(
        symbol=row.get("symbol", ""), setup=row.get("setup_name", ""), direction=row["direction"],
        ts=int(row["timestamp_entry"]), gross_bps=g, net_bps=g - round_trip_bps,
        risk_bps=risk * 1e4, gross_r=(row["gross_return"] / risk) if risk else 0.0,
        mfe_r=mfe / risk if risk else 0.0, mae_r=mae / risk if risk else 0.0,
        exit_reason=row.get("exit_reason") or "", session=_session(int(row["timestamp_entry"])),
        vol=vol, with_trend=with_trend, forward_bps=fwd)


def group_table(diags: list[TradeDiag], key, label: str,
                can_lead: bool = True) -> tuple[list[str], int, list[str]]:
    """Rows of one breakdown, the number of cells, and any leads.

    ``can_lead=False`` for anything only known after the trade (the exit
    reason): it describes the result, it cannot select a trade.
    """
    groups: dict[str, list[TradeDiag]] = {}
    for d in diags:
        groups.setdefault(str(key(d)), []).append(d)
    lines = [f"  {label:<22}{'n':>6}{'win%':>7}{'gross':>9}{'net':>9}{'t(net)':>8}{'avgR':>7}"]
    leads: list[str] = []
    for name, ds in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        net = [d.net_bps for d in ds]
        t = t_stat(net)
        lines.append(f"  {name:<22}{len(ds):>6}{100 * mean([d.gross_bps > 0 for d in ds]):>6.1f}%"
                     f"{mean([d.gross_bps for d in ds]):>+9.2f}{mean(net):>+9.2f}{t:>+8.2f}"
                     f"{mean([d.gross_r for d in ds]):>+7.2f}")
        # Net-positive only: a cell that loses significantly is not a lead,
        # because the reverse trade pays the same costs on ~zero gross.
        if can_lead and len(ds) >= LEAD_MIN_N and t >= LEAD_MIN_T:
            leads.append(f"{label}={name}: n={len(ds)}, net {mean(net):+.2f} bps, t {t:+.2f}")
    return lines, len(groups), leads


def report(hid: str, diags: list[TradeDiag], round_trip_bps: float, skipped: int) -> str:
    if not diags:
        return f"{hid}: no trades to diagnose"
    out = [f"diagnosis       {hid}  (research window only; no registry row written)",
           f"trades          {len(diags)} analysed, {skipped} skipped (no candle/notional)",
           f"cost            round trip {round_trip_bps:.1f} bps",
           f"win rate        {100 * mean([d.gross_bps > 0 for d in diags]):.1f}% gross, "
           f"{100 * mean([d.net_bps > 0 for d in diags]):.1f}% after costs",
           f"mean            gross {mean([d.gross_bps for d in diags]):+.2f} bps, "
           f"net {mean([d.net_bps for d in diags]):+.2f} bps, "
           f"median risk {median([d.risk_bps for d in diags]):.1f} bps "
           f"(= {median([d.risk_bps for d in diags]) / round_trip_bps:.1f}x the round trip)", ""]

    losers = [d for d in diags if d.gross_bps <= 0]
    winners = [d for d in diags if d.gross_bps > 0]
    out.append("excursions (how far price went before the trade closed)")
    if losers:
        out.append(f"  losers that were >= +1R in profit first: "
                   f"{100 * mean([d.mfe_r >= 1 for d in losers]):.1f}%   "
                   f">= +0.5R: {100 * mean([d.mfe_r >= 0.5 for d in losers]):.1f}%   "
                   f"median MFE {median([d.mfe_r for d in losers]):+.2f}R")
    if winners:
        out.append(f"  winners that went >= -0.8R against first: "
                   f"{100 * mean([d.mae_r >= 0.8 for d in winners]):.1f}%   "
                   f"median MAE {median([d.mae_r for d in winners]):.2f}R")
    out.append("")
    out.append("same entries held for a fixed number of bars (direction applied, gross)")
    out.append(f"  {'bars':>6}{'n':>7}{'mean bps':>10}{'t':>8}{'beats cost?':>13}")
    for h in HORIZONS:
        xs = [d.forward_bps[h] for d in diags if d.forward_bps[h] is not None]
        if xs:
            m = mean(xs)
            out.append(f"  {h:>6}{len(xs):>7}{m:>+10.2f}{t_stat(xs):>+8.2f}"
                       f"{'yes' if m > round_trip_bps else 'no':>13}")
    out.append("")

    vols = sorted(d.vol for d in diags)
    cut1, cut2 = vols[len(vols) // 3], vols[2 * len(vols) // 3]

    def vol_bucket(d: TradeDiag) -> str:
        return "low" if d.vol < cut1 else ("mid" if d.vol < cut2 else "high")

    cells, leads = 0, []
    for label, key in (("symbol", lambda d: d.symbol), ("setup", lambda d: d.setup),
                       ("direction", lambda d: d.direction), ("session", lambda d: d.session),
                       ("volatility tercile", vol_bucket),
                       ("with 4h-ish trend", lambda d: {True: "with", False: "against",
                                                        None: "n/a"}[d.with_trend])):
        lines, n, found = group_table(diags, key, label)
        out += [f"by {label}"] + lines + [""]
        cells += n
        leads += found
    # Known only after the trade ended: shown to explain the result, never a lead.
    lines, _, _ = group_table(diags, lambda d: d.exit_reason, "exit reason", can_lead=False)
    out += ["by exit reason (outcome, not a filter: never a lead)"] + lines + [""]
    out.append(f"cells that could lead: {cells}. A lead needs n >= {LEAD_MIN_N} and t(net) >= "
               f"+{LEAD_MIN_T}; with {cells} cells, about {cells / 740:.2f} would clear that "
               "by chance alone.")
    out.append("leads (each must become a NEW pre-declared hypothesis before it means anything):")
    out += [f"  - {x}" for x in leads] or ["  none"]
    return "\n".join(out)


CSV_HEADER = ["symbol", "setup", "direction", "ts", "gross_bps", "net_bps", "risk_bps",
              "gross_r", "mfe_r", "mae_r", "exit_reason", "session", "vol", "with_trend"] + \
    [f"fwd_{h}" for h in HORIZONS]


def _stem(hid: str, symbols: list[str] | None) -> str:
    """One coin at a time can run in parallel; each writes its own files."""
    return f"{hid}_{symbols[0]}" if symbols and len(symbols) == 1 else hid


def load_csv(path: str) -> list[TradeDiag]:
    """Read back a ``*_trades.csv`` written by ``diagnose``."""
    def num(r: dict, k: str) -> float | None:
        return None if r[k] in ("", "None") else float(r[k])

    out: list[TradeDiag] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(TradeDiag(
                symbol=r["symbol"], setup=r["setup"], direction=r["direction"], ts=int(r["ts"]),
                gross_bps=float(r["gross_bps"]), net_bps=float(r["net_bps"]),
                risk_bps=float(r["risk_bps"]), gross_r=float(r["gross_r"]),
                mfe_r=float(r["mfe_r"]), mae_r=float(r["mae_r"]), exit_reason=r["exit_reason"],
                session=r["session"], vol=float(r["vol"]),
                with_trend={"True": True, "False": False}.get(r["with_trend"]),
                forward_bps={h: num(r, f"fwd_{h}") for h in HORIZONS}))
    return out


def merge(hid: str, symbols: list[str], out_dir: str = "data/diagnostics") -> str:
    """Combine per-coin diagnoses (run in parallel) into the full report."""
    from maya.research.hypotheses import CATALOG
    rt = CATALOG[hid].cost_model.round_trip_bps
    diags: list[TradeDiag] = []
    for s in symbols:
        path = os.path.join(out_dir, f"{_stem(hid, [s])}_trades.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} missing: run diagnose {hid} --symbols {s} first")
        diags += load_csv(path)
    text = report(hid, diags, rt, 0)
    with open(os.path.join(out_dir, f"{hid}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return text


def diagnose(hid: str, research: dict[str, list[Candle]], out_dir: str = "data/diagnostics"
             ) -> str:
    """Replay a pipeline hypothesis on research candles and explain the result."""
    from maya.research.hypotheses import CATALOG, PIPELINE_CONFIGS, pipeline_trades
    if hid not in PIPELINE_CONFIGS:
        raise ValueError(f"{hid} does not run the pipeline; diagnose supports: "
                         f"{', '.join(sorted(PIPELINE_CONFIGS))}")
    tf, overrides = PIPELINE_CONFIGS[hid]
    rt = CATALOG[hid].cost_model.round_trip_bps
    rows, _ = pipeline_trades(research, tf, **overrides)
    # Bars in roughly four hours, for the trend-context column.
    bar_min = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(tf, 15)
    trend_bars = max(1, 240 // bar_min)
    times = {s: [c.timestamp for c in cs] for s, cs in research.items()}
    diags, skipped = [], 0
    for r in rows:
        sym = r.get("symbol")
        d = (diagnose_trade(r, research[sym], times[sym], rt, trend_bars)
             if sym in research else None)
        if d is None:
            skipped += 1
        else:
            diags.append(d)
    text = report(hid, diags, rt, skipped)
    stem = _stem(hid, sorted(research))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{stem}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    with open(os.path.join(out_dir, f"{stem}_trades.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for d in diags:
            w.writerow([d.symbol, d.setup, d.direction, d.ts, round(d.gross_bps, 3),
                        round(d.net_bps, 3), round(d.risk_bps, 3), round(d.gross_r, 4),
                        round(d.mfe_r, 4), round(d.mae_r, 4), d.exit_reason, d.session,
                        round(d.vol, 6), d.with_trend]
                       + [None if d.forward_bps[h] is None else round(d.forward_bps[h], 3)
                          for h in HORIZONS])
    return text
