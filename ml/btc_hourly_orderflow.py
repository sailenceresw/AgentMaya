"""Hourly BTC perp model with real order flow + funding -- walk-forward test.

Separate from the ``maya`` package. Reads the caches ``maya.data.download``
writes (klines with Binance's taker-buy column, funding history), so the
order-flow features are measured, not reconstructed.

Question: does real order flow (delta/CVD) plus funding predict the next
H hours of BTCUSDT perp returns well enough to pay fees and funding?

Rules fixed before looking at results (do not tune them on the output):
- Features at bar t use only data known at bar t's close (funding rates only
  once their funding_time has passed).
- Decisions every H bars, non-overlapping; hold H bars.
- long if P(up) > 0.55, short if < 0.45, else flat.
- Walk-forward, monthly retrain, expanding window, first 12 months train only.
  The last H bars before each test month are purged from training so no
  training label overlaps the test period.
- Costs: taker fee per side on every position change (default 0.05%,
  Binance USD-M) plus the funding actually paid/received while holding.

History of runs (so the holdout means something):
- Run 1, H=4, 2023-2025 (test 2024-2025): AUC 0.55, gross +74%, net -57%
  (fees ate it). Seen.
- Run 2, H=24, pre-registered 2026-11-17 BEFORE any 2026 bar was loaded:
  same features, models, thresholds, retrain schedule and fee as run 1; only
  H changes (fewer trades -> less fee drag). Primary verdict = taker fee.
  Maker fee 0.02% is reported as sensitivity only, not as the verdict.
  2026-01-01 onward is a holdout: the script drops it unless
  --unlock-holdout is passed, and it is unlocked once, after the 2024-2025
  development result is written down. No change after unlocking.

Run:  python ml/btc_hourly_orderflow.py --klines data/klines_...csv --funding data/funding_...csv
      [--horizon 24] [--unlock-holdout]   (several --klines/--funding files are concatenated)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

LONG_T, SHORT_T = 0.55, 0.45
TRAIN_MONTHS = 12
SEED = 7
BAR_MS = 3_600_000
HOLDOUT_START = "2026-01-01"
MAKER_FEE = 0.0002  # sensitivity only


def load(klines: list[str], funding: list[str], cutoff_ms: int | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    k = pd.concat([pd.read_csv(p) for p in klines])
    if k["taker_buy_volume"].isna().any():
        raise SystemExit("klines lack taker_buy_volume -- order flow would be fake; re-download")
    k = k.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    f = pd.concat([pd.read_csv(p) for p in funding])
    f = f.drop_duplicates("funding_time").sort_values("funding_time").reset_index(drop=True)
    if cutoff_ms is not None:
        # Locked holdout: the data is cut before features or labels exist, so
        # nothing (not even a dev-period label's last hours) sees it.
        k = k[k["timestamp"] + BAR_MS <= cutoff_ms].reset_index(drop=True)
        f = f[f["funding_time"] < cutoff_ms].reset_index(drop=True)
    gaps = k["timestamp"].diff().dropna()
    if (gaps != BAR_MS).any():
        print(f"warning: {(gaps != BAR_MS).sum()} gaps in hourly bars")
    return k, f


def features(k: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    c, v, tbv = k["close"], k["volume"], k["taker_buy_volume"]
    lr = np.log(c).diff()
    delta = 2 * tbv - v  # aggressive buy minus aggressive sell volume
    X = pd.DataFrame(index=k.index)
    for n in (1, 4, 12, 24, 72):
        X[f"ret_{n}h"] = np.log(c / c.shift(n))
    X["vol_24h"] = lr.rolling(24).std()
    X["vol_168h"] = lr.rolling(168).std()
    X["vol_ratio"] = X["vol_24h"] / X["vol_168h"]
    X["delta_ratio_1h"] = delta / v
    for n in (4, 12, 24, 72):
        X[f"cvd_{n}h"] = delta.rolling(n).sum() / v.rolling(n).sum()
    # Divergence: price up while net aggression is selling, or vice versa.
    X["div_24h"] = np.sign(X["ret_24h"]) - np.sign(X["cvd_24h"])
    lv = np.log(v.replace(0, np.nan))
    X["volume_z168"] = (lv - lv.rolling(168).mean()) / lv.rolling(168).std()
    for n in (24, 168):
        X[f"dist_sma{n}"] = c / c.rolling(n).mean() - 1
    X["range_pct"] = (k["high"] - k["low"]) / c

    # Funding known at bar close = latest funding_time <= bar close time.
    close_t = k["timestamp"] + BAR_MS
    idx = np.searchsorted(f["funding_time"].values, close_t.values, side="right") - 1
    rates = f["funding_rate"].values
    ok = idx >= 0
    X["funding_last"] = np.where(ok, rates[np.clip(idx, 0, None)], np.nan)
    X["funding_avg3"] = np.where(idx >= 2, [rates[max(i - 2, 0):i + 1].mean() for i in idx], np.nan)
    X["funding_chg"] = np.where(idx >= 1, rates[np.clip(idx, 0, None)] - rates[np.clip(idx - 1, 0, None)], np.nan)

    dt = pd.to_datetime(close_t, unit="ms", utc=True)
    X["hour"] = dt.dt.hour
    X["dow"] = dt.dt.day_of_week
    return X


def funding_paid(k: pd.DataFrame, f: pd.DataFrame, h: int) -> pd.Series:
    """Sum of funding rates settling inside each h-bar hold starting at bar t's
    close. A long pays +rate, a short receives it."""
    start = (k["timestamp"] + BAR_MS).values
    end = start + h * BAR_MS
    ft, fr = f["funding_time"].values, f["funding_rate"].values
    cum = np.concatenate([[0.0], np.cumsum(fr)])
    lo = np.searchsorted(ft, start, side="right")
    hi = np.searchsorted(ft, end, side="right")
    return pd.Series(cum[hi] - cum[lo], index=k.index)


def models() -> dict:
    return {
        "gbm": HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.03, max_iter=200, min_samples_leaf=200,
            l2_regularization=1.0, random_state=SEED),
        "logit": make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000)),
    }


def walk_forward(X: pd.DataFrame, y: pd.Series, month: pd.Series, dec: pd.Series, h: int) -> pd.DataFrame:
    months = sorted(month.unique())
    out = []
    for m in months[TRAIN_MONTHS:]:
        test = (month == m) & dec
        first_test = test[test].index.min()
        if pd.isna(first_test):
            continue
        # Purge: training labels look h bars ahead, so drop the last h rows
        # before the test month.
        train = (month < m) & (X.index < first_test - h)
        row = pd.DataFrame(index=X.index[test])
        for name, mdl in models().items():
            mdl.fit(X[train], y[train])
            row[f"p_{name}"] = mdl.predict_proba(X[test])[:, 1]
        out.append(row)
    return pd.concat(out)


def stats(r: pd.Series, per_year: float) -> dict:
    lr = np.log1p(r)
    eq = np.exp(lr.cumsum())
    sharpe = r.mean() / r.std() * np.sqrt(per_year) if r.std() > 0 else 0.0
    return {"total_return_%": round(float(eq.iloc[-1] - 1) * 100, 1),
            "sharpe": round(float(sharpe), 2),
            "max_drawdown_%": round(float((eq / eq.cummax() - 1).min()) * 100, 1)}


def positions(p: pd.Series) -> pd.Series:
    return pd.Series(np.where(p > LONG_T, 1.0, np.where(p < SHORT_T, -1.0, 0.0)), index=p.index)


def net_returns(pos: pd.Series, rt: pd.Series, ft: pd.Series, fee: float) -> tuple[pd.Series, pd.Series]:
    cost = pos.diff().abs().fillna(pos.abs()) * fee
    return pos * rt - pos * ft - cost, cost


def evaluate(preds: pd.DataFrame, y: pd.Series, rt_all: pd.Series, ft_all: pd.Series,
             ts: pd.Series, fee: float, per_year: float) -> dict:
    yt, rt, ft = y.loc[preds.index], rt_all.loc[preds.index], ft_all.loc[preds.index]
    seg: dict = {
        "period": f"{ts.loc[preds.index[0]]} -> {ts.loc[preds.index[-1]]}",
        "decisions": len(preds), "up_rate": round(float(yt.mean()), 4),
        "long_perp_hold": stats(rt - ft, per_year), "models": {}}
    for name in ("gbm", "logit"):
        p = preds[f"p_{name}"]
        pos = positions(p)
        r, cost = net_returns(pos, rt, ft, fee)
        r_maker, _ = net_returns(pos, rt, ft, MAKER_FEE)
        traded = pos != 0
        hits = int(((pos[traded] > 0) == (yt[traded] == 1)).sum())
        q = ts.loc[r.index].dt.to_period("Q").astype(str).values
        seg["models"][name] = {
            "auc": round(float(roc_auc_score(yt, p)), 4),
            "traded_%": round(float(traded.mean()) * 100, 1),
            "position_changes": int(pos.diff().abs().fillna(pos.abs()).gt(0).sum()),
            "hit_rate_when_traded": round(hits / max(int(traded.sum()), 1), 4),
            "fees_paid_%": round(float(cost.sum()) * 100, 1),
            "funding_paid_%": round(float((pos * ft).sum()) * 100, 2),
            "gross_return_%_no_costs": round(float(np.expm1(np.log1p(pos * rt).sum())) * 100, 1),
            **stats(r, per_year),
            "sensitivity_total_return_%_at_maker_fee": stats(r_maker, per_year)["total_return_%"],
            "return_by_quarter_%": {qq: stats(r[q == qq], per_year)["total_return_%"]
                                    for qq in sorted(set(q))},
        }
    return seg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", required=True, nargs="+")
    ap.add_argument("--funding", required=True, nargs="+")
    ap.add_argument("--fee", type=float, default=0.0005, help="taker fee per side")
    ap.add_argument("--horizon", type=int, default=4, help="hold / decision spacing, hours")
    ap.add_argument("--unlock-holdout", action="store_true",
                    help=f"include data from {HOLDOUT_START} on (do this once, rules frozen)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    h = a.horizon
    holdout_ms = int(pd.Timestamp(HOLDOUT_START, tz="UTC").timestamp() * 1000)
    out = Path(a.out or f"ml/output_{h}h" + ("_holdout" if a.unlock_holdout else ""))
    out.mkdir(parents=True, exist_ok=True)

    k, f = load(a.klines, a.funding, None if a.unlock_holdout else holdout_ms)
    X = features(k, f)
    fwd = np.log(k["close"].shift(-h) / k["close"])
    y = (fwd > 0).astype(int)
    fund = funding_paid(k, f, h)
    ok = X.notna().all(axis=1) & fwd.notna()
    X, y, fwd, fund = X[ok], y[ok], fwd[ok], fund[ok]
    ts = pd.to_datetime(k.loc[X.index, "timestamp"], unit="ms", utc=True)
    month = ts.dt.strftime("%Y-%m")
    dec = pd.Series((np.arange(len(X)) % h) == 0, index=X.index)  # non-overlapping decisions
    print(f"Data: {ts.iloc[0]} -> {ts.iloc[-1]}, {len(X)} hourly rows, {X.shape[1]} features, "
          f"H={h}, holdout {'UNLOCKED' if a.unlock_holdout else 'locked'}")

    preds = walk_forward(X, y, month, dec, h)
    rt, per_year = np.expm1(fwd), 365 * 24 / h
    is_hold = ts.loc[preds.index] >= pd.Timestamp(HOLDOUT_START, tz="UTC")

    report: dict = {"horizon_h": h, "fee_per_side": a.fee, "maker_fee_sensitivity": MAKER_FEE,
                    "thresholds": [SHORT_T, LONG_T], "holdout_unlocked": a.unlock_holdout,
                    "development": evaluate(preds[~is_hold.values], y, rt, fund, ts, a.fee, per_year)}
    if is_hold.any():
        report["holdout"] = evaluate(preds[is_hold.values], y, rt, fund, ts, a.fee, per_year)

    rng = np.random.default_rng(SEED)
    y_shuf = pd.Series(rng.permutation(y.values), index=y.index)
    shuf = walk_forward(X, y_shuf, month, dec, h)
    report["leak_check_shuffled_auc_gbm"] = round(float(
        roc_auc_score(y_shuf.loc[shuf.index], shuf["p_gbm"])), 4)

    # What the trees leaned on (fit on all data -- descriptive only, not a test).
    from sklearn.inspection import permutation_importance
    g = models()["gbm"].fit(X[dec], y[dec])
    imp = permutation_importance(g, X[dec], y[dec], n_repeats=3, random_state=SEED, scoring="roc_auc")
    report["top_features_in_sample"] = dict(sorted(
        zip(X.columns, np.round(imp.importances_mean, 4)), key=lambda t: -t[1])[:8])

    rt_p, ft_p = rt.loc[preds.index], fund.loc[preds.index]
    preds.assign(fwd_ret=rt_p, funding=ft_p, up=y.loc[preds.index]).to_csv(out / "walk_forward_predictions.csv")
    (out / "report.json").write_text(json.dumps(report, indent=2))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5))
        when = ts.loc[preds.index].values
        ax.plot(when, np.exp(np.log1p(rt_p - ft_p).cumsum()), label="long perp (hold)")
        for name in ("gbm", "logit"):
            r, _ = net_returns(positions(preds[f"p_{name}"]), rt_p, ft_p, a.fee)
            ax.plot(when, np.exp(np.log1p(r).cumsum()), label=name)
        if is_hold.any():
            ax.axvline(pd.Timestamp(HOLDOUT_START), color="k", ls="--", lw=1, label="holdout starts")
        ax.set_title(f"Walk-forward equity, BTCUSDT perp, {h}h hold, fee {a.fee*100:.2f}%/side + funding")
        ax.legend()
        ax.grid(alpha=.3)
        fig.tight_layout()
        fig.savefig(out / "equity.png", dpi=120)
    except Exception as exc:  # noqa: BLE001
        print("plot skipped:", exc)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
