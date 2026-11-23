"""Long-history, price/volume-only version of the H=24 hourly model.

Data: the user's own 1-minute BTC/USD file (Timestamp,Open,High,Low,Close,
Volume; 2012 -> 2026, looks like Bitstamp spot), split into parts. It has no
taker-buy volume and no funding, so the order-flow (CVD) and funding
features are DROPPED, not faked.

Why: the H=24 development result (2024-2025, Binance perp) rests on ~730
decisions in one bull market. This asks whether the price/volume part of the
same model holds up across ~12 years of very different regimes.

Rules fixed 2026-11-17, before this script was first run:
- Same models, thresholds (0.45/0.55), H=24 non-overlapping decisions,
  monthly expanding walk-forward with H-bar purge, first 12 months train only
  -- all imported from btc_hourly_orderflow, nothing re-tuned.
- Features: that model's features minus delta/CVD/divergence/funding.
- Data from 2013-01-01 (2012 is thin, many zero-volume minutes), so the
  first test month is 2014-01.
- Hourly bars: open=first, high=max, low=min, close=last, volume=sum of the
  minutes in the hour. Hours with no minutes stay missing; any feature window
  touching a gap is NaN and that row is dropped (no forward-filled prices).
- Costs: 0.05%/side taker fee on every position change, as in the perp
  model. No funding (none for spot; the perp did not exist before 2019) --
  this flatters the result slightly versus trading a perp.
- 2026-01-01 onward is the holdout, cut out at load time unless
  --unlock-holdout. Same single-unlock rule as the order-flow model.

Run:  python ml/btc_longhistory_price.py --parts "C:/Users/<you>/Videos/btcusd_1-min_data_parts"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
import btc_hourly_orderflow as m  # noqa: E402

H = 24
START = "2013-01-01"
DROP = ["delta_ratio_1h", "cvd_4h", "cvd_12h", "cvd_24h", "cvd_72h", "div_24h",
        "funding_last", "funding_avg3", "funding_chg"]


def hourly_from_minutes(parts_dir: Path, cache: Path) -> pd.DataFrame:
    if cache.exists():
        return pd.read_csv(cache)
    files = sorted(parts_dir.glob("*.csv"))
    if not files:
        raise SystemExit(f"no CSV parts in {parts_dir}")
    mins = pd.concat([pd.read_csv(p) for p in files]).drop_duplicates("Timestamp")
    mins = mins.sort_values("Timestamp")
    mins["hour"] = (mins["Timestamp"] // 3600) * 3600
    g = mins.groupby("hour")
    k = pd.DataFrame({"open": g["Open"].first(), "high": g["High"].max(), "low": g["Low"].min(),
                      "close": g["Close"].last(), "volume": g["Volume"].sum(),
                      "minutes": g.size()})
    k.index = k.index * 1000  # ms, like the Binance cache
    k = k.rename_axis("timestamp").reset_index()
    cache.parent.mkdir(parents=True, exist_ok=True)
    k.to_csv(cache, index=False)
    print(f"built {cache} from {len(files)} parts, {len(mins)} minutes -> {len(k)} hours")
    return k


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", required=True, help="folder with the 1-minute CSV parts")
    ap.add_argument("--cache", default="data/btcusd_1min_parts_1h.csv")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--unlock-holdout", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = Path(a.out or "ml/output_longhistory_24h" + ("_holdout" if a.unlock_holdout else ""))
    out.mkdir(parents=True, exist_ok=True)

    k = hourly_from_minutes(Path(a.parts), Path(a.cache))
    start_ms = int(pd.Timestamp(START, tz="UTC").timestamp() * 1000)
    hold_ms = int(pd.Timestamp(m.HOLDOUT_START, tz="UTC").timestamp() * 1000)
    k = k[k["timestamp"] >= start_ms]
    if not a.unlock_holdout:
        k = k[k["timestamp"] + m.BAR_MS <= hold_ms]
    # Put bars on a full hourly grid so shift(n) means n hours, not n rows;
    # missing hours are NaN and poison every window that touches them.
    grid = np.arange(k["timestamp"].min(), k["timestamp"].max() + 1, m.BAR_MS)
    missing = len(grid) - len(k)
    k = k.set_index("timestamp").reindex(grid).rename_axis("timestamp").reset_index()
    k["taker_buy_volume"] = np.nan  # not in this data; the features using it are dropped
    # No funding in this data. One placeholder row far in the future (an empty
    # frame crashes the indexing in features()): every bar precedes it, so the
    # funding features are NaN (and dropped) and funding paid is 0.
    f = pd.DataFrame({"funding_time": [np.int64(2**62)], "funding_rate": [0.0]})

    X = m.features(k, f).drop(columns=DROP)
    fwd = np.log(k["close"].shift(-H) / k["close"])
    y = (fwd > 0).astype(int)
    fund = m.funding_paid(k, f, H)  # all zero: no funding in this data
    ok = X.notna().all(axis=1) & fwd.notna()
    X, y, fwd, fund = X[ok], y[ok], fwd[ok], fund[ok]
    ts = pd.to_datetime(k.loc[X.index, "timestamp"], unit="ms", utc=True)
    month = ts.dt.strftime("%Y-%m")
    # Decisions at one fixed UTC hour (00:00) so a gap cannot shift the
    # decision clock; still non-overlapping 24h holds.
    dec = pd.Series((ts.dt.hour == 0).values, index=X.index)
    print(f"Data: {ts.iloc[0]} -> {ts.iloc[-1]}, {len(X)} usable hourly rows "
          f"({missing} missing hours on the grid), {X.shape[1]} features, "
          f"holdout {'UNLOCKED' if a.unlock_holdout else 'locked'}")

    preds = m.walk_forward(X, y, month, dec, H)
    rt, per_year = np.expm1(fwd), 365 * 24 / H
    is_hold = (ts.loc[preds.index] >= pd.Timestamp(m.HOLDOUT_START, tz="UTC")).values

    report: dict = {"source": "user 1-min BTC/USD parts (spot, no order flow, no funding)",
                    "horizon_h": H, "fee_per_side": a.fee, "features": list(X.columns),
                    "development": m.evaluate(preds[~is_hold], y, rt, fund, ts, a.fee, per_year)}
    if is_hold.any():
        report["holdout"] = m.evaluate(preds[is_hold], y, rt, fund, ts, a.fee, per_year)

    # Year-by-year view (the quarterly one is long over 12 years).
    yr = ts.loc[preds.index].dt.year.values
    rt_p = rt.loc[preds.index]
    years: dict = {}
    for name in ("gbm", "logit"):
        r, _ = m.net_returns(m.positions(preds[f"p_{name}"]), rt_p, fund.loc[preds.index], a.fee)
        years[name] = {str(v): m.stats(r[yr == v], per_year)["total_return_%"] for v in sorted(set(yr))}
        aucs = {}
        for v in sorted(set(yr)):
            s = yr == v
            aucs[str(v)] = round(float(roc_auc_score(y.loc[preds.index][s], preds[f"p_{name}"][s])), 3)
        years[f"{name}_auc"] = aucs
    years["buy_and_hold"] = {str(v): m.stats(rt_p[yr == v], per_year)["total_return_%"]
                             for v in sorted(set(yr))}
    report["by_year"] = years

    rng = np.random.default_rng(m.SEED)
    y_shuf = pd.Series(rng.permutation(y.values), index=y.index)
    shuf = m.walk_forward(X, y_shuf, month, dec, H)
    report["leak_check_shuffled_auc"] = {
        n: round(float(roc_auc_score(y_shuf.loc[shuf.index], shuf[f"p_{n}"])), 4) for n in ("gbm", "logit")}

    preds.assign(fwd_ret=rt_p, up=y.loc[preds.index]).to_csv(out / "walk_forward_predictions.csv")
    (out / "report.json").write_text(json.dumps(report, indent=2))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(11, 5))
        when = ts.loc[preds.index].values
        ax.plot(when, np.exp(np.log1p(rt_p).cumsum()), label="buy & hold")
        for name in ("gbm", "logit"):
            r, _ = m.net_returns(m.positions(preds[f"p_{name}"]), rt_p, fund.loc[preds.index], a.fee)
            ax.plot(when, np.exp(np.log1p(r).cumsum()), label=name)
        ax.set_yscale("log")
        ax.set_title(f"Walk-forward, BTC/USD price-only, {H}h hold, fee {a.fee*100:.2f}%/side (log)")
        ax.legend()
        ax.grid(alpha=.3)
        fig.tight_layout()
        fig.savefig(out / "equity.png", dpi=120)
    except Exception as exc:  # noqa: BLE001
        print("plot skipped:", exc)
    print(json.dumps({k_: v for k_, v in report.items() if k_ != "features"}, indent=2))


if __name__ == "__main__":
    main()
