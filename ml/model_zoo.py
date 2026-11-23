"""Model zoo: every reasonable model family on the same H=24 BTC test.

Rules fixed 2026-11-17, BEFORE any zoo model was run (do not tune on output):

Datasets (both with 2026 locked out unless --unlock-holdout):
- perp: Binance BTCUSDT perp 2023-2025, all 23 features incl. real order flow
  (CVD) and funding; test 2024-01 -> 2025-12.
- long: the user's 1-min BTC/USD parts, price/volume-only features (14);
  test 2014-01 -> 2025-12. No funding.

Models (hyperparameters fixed here, never searched):
- logit      L2 logistic regression (the existing baseline)
- gbm        histogram gradient boosting (sklearn's LightGBM-style trees)
- rf         random forest, 300 trees, depth 8, leaf >= 200
- et         extra trees, same shape
- mlp        neural net 64-32 ReLU, L2 1e-3, early stopping
- ensemble   mean probability of the five above
- gbm_reg    gradient-boosting REGRESSION of the 24h return itself;
             long if predicted return > +0.10% (one taker round trip),
             short if < -0.10%, else flat
Classifiers: long if P(up) > 0.55, short if < 0.45, else flat.

Protocol: monthly expanding walk-forward, first 12 months train only, last
H hours before each test month purged. Training uses every 2nd hour (labels
24h ahead overlap heavily, so this loses little and halves fit time).
Decisions every 24h, non-overlapping. Taker fee 0.05%/side + funding (perp).

Judging (also fixed now):
- Per model: AUC with an analytic z-test, and a timing test (shuffle the
  model's own positions 5,000x, compare gross return).
- Multiple testing: 7 zoo models x 2 datasets + the 4 earlier runs = 18
  tries. A model "passes" development only if its timing p < 0.05/18.
- The 2026 holdout primary is the logit perp model (it passed its dev
  nulls before the zoo existed). Zoo models that pass development are
  secondary on the holdout. The holdout is unlocked once.

Run:  python ml/model_zoo.py --dataset perp
      python ml/model_zoo.py --dataset long --parts "C:/Users/<you>/Videos/btcusd_1-min_data_parts"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from math import erf, sqrt
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import (ExtraTreesClassifier, HistGradientBoostingClassifier,
                              HistGradientBoostingRegressor, RandomForestClassifier)
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).parent))
import btc_hourly_orderflow as m  # noqa: E402
import btc_longhistory_price as lh  # noqa: E402

H = 24
SEED = 7
REG_T = 0.001  # one taker round trip
N_TRIES = 20  # 18 + the two sequence models (cnn, lstm) added the same day; stricter only
CLASSIFIERS = ("logit", "gbm", "rf", "et", "mlp")


def classifiers() -> dict:
    tree = dict(n_estimators=300, max_depth=8, min_samples_leaf=200, max_features="sqrt",
                n_jobs=-1, random_state=SEED)
    return {
        "logit": make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000)),
        "gbm": HistGradientBoostingClassifier(max_depth=3, learning_rate=0.03, max_iter=200,
                                              min_samples_leaf=200, l2_regularization=1.0,
                                              random_state=SEED),
        "rf": RandomForestClassifier(**tree),
        "et": ExtraTreesClassifier(**tree),
        "mlp": make_pipeline(StandardScaler(), MLPClassifier(
            hidden_layer_sizes=(64, 32), alpha=1e-3, learning_rate_init=1e-3, max_iter=200,
            early_stopping=True, validation_fraction=0.15, n_iter_no_change=10,
            random_state=SEED)),
    }


def regressor():
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=200,
                                         min_samples_leaf=200, l2_regularization=1.0,
                                         random_state=SEED)


def build(dataset: str, parts: str | None, unlock: bool):
    hold_ms = int(pd.Timestamp(m.HOLDOUT_START, tz="UTC").timestamp() * 1000)
    if dataset == "perp":
        kl = ["data/klines_BTCUSDT_1h_2023-01-01_2025-12-31.csv"]
        fu = ["data/funding_BTCUSDT_2023-01-01_2025-12-31.csv"]
        if unlock:
            kl.append("data/klines_BTCUSDT_1h_2026-01-01_2026-11-16.csv")
            fu.append("data/funding_BTCUSDT_2026-01-01_2026-11-16.csv")
        k, f = m.load(kl, fu, None if unlock else hold_ms)
        X = m.features(k, f)
    else:
        if not parts:
            raise SystemExit("--parts is required for --dataset long")
        k = lh.hourly_from_minutes(Path(parts), Path("data/btcusd_1min_parts_1h.csv"))
        k = k[k["timestamp"] >= int(pd.Timestamp(lh.START, tz="UTC").timestamp() * 1000)]
        if not unlock:
            k = k[k["timestamp"] + m.BAR_MS <= hold_ms]
        grid = np.arange(k["timestamp"].min(), k["timestamp"].max() + 1, m.BAR_MS)
        k = k.set_index("timestamp").reindex(grid).rename_axis("timestamp").reset_index()
        k["taker_buy_volume"] = np.nan
        f = pd.DataFrame({"funding_time": [np.int64(2**62)], "funding_rate": [0.0]})
        X = m.features(k, f).drop(columns=lh.DROP)
    fwd = np.log(k["close"].shift(-H) / k["close"])
    fund = m.funding_paid(k, f, H)
    ok = X.notna().all(axis=1) & fwd.notna()
    X, fwd, fund = X[ok], fwd[ok], fund[ok]
    ts = pd.to_datetime(k.loc[X.index, "timestamp"], unit="ms", utc=True)
    if dataset == "perp":  # same decision clock as the earlier perp runs
        dec = pd.Series((np.arange(len(X)) % H) == 0, index=X.index)
    else:
        dec = pd.Series((ts.dt.hour == 0).values, index=X.index)
    return X, fwd, fund, ts, dec


def walk_forward(X, y, fwd, month, dec, ts) -> pd.DataFrame:
    thin = pd.Series((ts.dt.hour % 2 == 0).values, index=X.index)
    out = []
    months = sorted(month.unique())
    for i, mo in enumerate(months[m.TRAIN_MONTHS:]):
        test = (month == mo) & dec
        first = test[test].index.min()
        if pd.isna(first):
            continue
        train = (month < mo) & (X.index < first - H) & thin
        row = pd.DataFrame(index=X.index[test])
        for name, mdl in classifiers().items():
            mdl.fit(X[train], y[train])
            row[f"p_{name}"] = mdl.predict_proba(X[test])[:, 1]
        row["p_ensemble"] = row[[f"p_{c}" for c in CLASSIFIERS]].mean(axis=1)
        row["r_gbm_reg"] = regressor().fit(X[train], fwd[train]).predict(X[test])
        out.append(row)
        if i % 12 == 0:
            print(f"  {mo} done", flush=True)
    return pd.concat(out)


def auc_p(auc: float, n1: int, n0: int) -> float:
    se = sqrt((n1 + n0 + 1) / (12 * n1 * n0))
    z = (auc - 0.5) / se
    return 0.5 * (1 - erf(z / sqrt(2)))  # one-sided


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["perp", "long"], required=True)
    ap.add_argument("--parts")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--unlock-holdout", action="store_true")
    a = ap.parse_args()
    out = Path(f"ml/output_zoo_{a.dataset}" + ("_holdout" if a.unlock_holdout else ""))
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    X, fwd, fund, ts, dec = build(a.dataset, a.parts, a.unlock_holdout)
    y = (fwd > 0).astype(int)
    month = ts.dt.strftime("%Y-%m")
    print(f"{a.dataset}: {ts.iloc[0]} -> {ts.iloc[-1]}, {len(X)} rows, {X.shape[1]} features, "
          f"holdout {'UNLOCKED' if a.unlock_holdout else 'locked'}", flush=True)
    preds = walk_forward(X, y, fwd, month, dec, ts)
    rt, ft = np.expm1(fwd.loc[preds.index]), fund.loc[preds.index]
    yt = y.loc[preds.index]
    per_year = 365
    is_hold = (ts.loc[preds.index] >= pd.Timestamp(m.HOLDOUT_START, tz="UTC")).values
    yr = ts.loc[preds.index].dt.year.values
    rng = np.random.default_rng(SEED)

    def score(sel: np.ndarray) -> dict:
        res: dict = {"decisions": int(sel.sum()), "up_rate": round(float(yt[sel].mean()), 4),
                     "buy_and_hold": m.stats(rt[sel] - ft[sel], per_year)}
        n1, n0 = int(yt[sel].sum()), int((1 - yt[sel]).sum())
        for name in (*CLASSIFIERS, "ensemble", "gbm_reg"):
            if name == "gbm_reg":
                s = preds["r_gbm_reg"][sel]
                pos = pd.Series(np.where(s > REG_T, 1.0, np.where(s < -REG_T, -1.0, 0.0)), index=s.index)
            else:
                s = preds[f"p_{name}"][sel]
                pos = m.positions(s)
            r, cost = m.net_returns(pos, rt[sel], ft[sel], a.fee)
            auc = float(roc_auc_score(yt[sel], s))
            g = lambda q: np.expm1(np.log1p(q * rt[sel].values).sum())  # noqa: E731
            g_real = g(pos.values)
            null = np.array([g(rng.permutation(pos.values)) for _ in range(5000)])
            p_t = (1 + (null >= g_real).sum()) / 5001
            res[name] = {
                "auc": round(auc, 4), "auc_p": round(auc_p(auc, n1, n0), 4),
                "traded_%": round(float((pos != 0).mean()) * 100, 1),
                "long_%": round(float((pos > 0).mean()) * 100, 1),
                "gross_%": round(float(g_real) * 100, 1),
                "timing_p": round(float(p_t), 4),
                "passes_corrected": bool(p_t < 0.05 / N_TRIES),
                "fees_%": round(float(cost.sum()) * 100, 1),
                **m.stats(r, per_year),
                "by_year_%": {str(v): m.stats(r[yr[sel] == v], per_year)["total_return_%"]
                              for v in sorted(set(yr[sel]))},
            }
        return res

    report = {"dataset": a.dataset, "features": list(X.columns), "fee_per_side": a.fee,
              "bonferroni_tries": N_TRIES, "development": score(~is_hold)}
    if is_hold.any():
        report["holdout"] = score(is_hold)

    print("shuffled-label control...", flush=True)
    y_s = pd.Series(np.random.default_rng(SEED).permutation(y.values), index=y.index)
    sh = walk_forward(X, y_s, fwd, month, dec, ts)
    report["shuffled_label_auc"] = {n: round(float(roc_auc_score(y_s.loc[sh.index], sh[f"p_{n}"])), 4)
                                    for n in (*CLASSIFIERS, "ensemble")}
    report["minutes"] = round((time.time() - t0) / 60, 1)

    preds.assign(fwd_ret=rt, funding=ft, up=yt).to_csv(out / "walk_forward_predictions.csv")
    (out / "report.json").write_text(json.dumps(report, indent=2))

    d = report["development"]
    print(f"\nDEVELOPMENT  buy&hold {d['buy_and_hold']['total_return_%']}%  "
          f"(dd {d['buy_and_hold']['max_drawdown_%']}%)")
    print(f"{'model':10} {'AUC':>6} {'aucP':>6} {'gross%':>8} {'timingP':>8} {'pass':>5} "
          f"{'fees%':>6} {'net%':>8} {'sharpe':>6} {'maxDD%':>7}")
    for name in (*CLASSIFIERS, "ensemble", "gbm_reg"):
        r = d[name]
        print(f"{name:10} {r['auc']:6.3f} {r['auc_p']:6.3f} {r['gross_%']:8.1f} {r['timing_p']:8.4f} "
              f"{'yes' if r['passes_corrected'] else 'no':>5} {r['fees_%']:6.1f} "
              f"{r['total_return_%']:8.1f} {r['sharpe']:6.2f} {r['max_drawdown_%']:7.1f}")
    print("shuffled-label AUC:", report["shuffled_label_auc"], f"| {report['minutes']} min")


if __name__ == "__main__":
    main()
