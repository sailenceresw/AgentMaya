"""Daily BTC direction model -- an honest walk-forward experiment.

Separate from the ``maya`` package on purpose: AgentMaya is a rule-based
intraday order-flow agent and this is a daily ML experiment on Yahoo OHLCV
(no taker-buy column, so no real order flow).

Question asked: can past daily price/volume predict whether BTC closes higher
tomorrow well enough to beat buy-and-hold after fees?

Honesty rules (decided before looking at any result):
- Features at day t use only data up to and including day t's close.
- Walk-forward: each test year is predicted by models trained only on earlier
  years. Nothing about a test year is ever used to fit or choose anything.
- Hyperparameters are fixed below and NOT tuned on the results.
- Fees are charged on every position change (default 0.25%/side, roughly
  Alpaca crypto taker). Trades fill at the daily close -- for a 24/7 market
  that is close to the next open, but it is still slightly optimistic.

Run:  python ml/btc_daily_model.py [--csv data/btc_daily/BTC-USD.csv] [--fee 0.0025]
"""
from __future__ import annotations

import argparse
import json
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

FIRST_TEST_YEAR = 2018  # 2015-2017 is the minimum training history
SEED = 7


def load(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["Date"]).set_index("Date").sort_index()
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    # Yahoo occasionally has null rows; drop rather than forward-fill so no
    # day pretends to have traded.
    return df.dropna()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def features(df: pd.DataFrame) -> pd.DataFrame:
    c, v = df["Close"], df["Volume"]
    r1 = np.log(c).diff()
    f = pd.DataFrame(index=df.index)
    for n in (1, 3, 7, 14, 30):
        f[f"ret_{n}d"] = np.log(c / c.shift(n))
    f["vol_7d"] = r1.rolling(7).std()
    f["vol_30d"] = r1.rolling(30).std()
    f["vol_ratio"] = f["vol_7d"] / f["vol_30d"]
    f["rsi_14"] = rsi(c)
    for n in (20, 50, 200):
        f[f"dist_sma{n}"] = c / c.rolling(n).mean() - 1
    f["range_pct"] = (df["High"] - df["Low"]) / c
    f["close_in_range"] = (c - df["Low"]) / (df["High"] - df["Low"]).replace(0, np.nan)
    lv = np.log(v.replace(0, np.nan))
    f["volume_z30"] = (lv - lv.rolling(30).mean()) / lv.rolling(30).std()
    f["dow"] = df.index.day_of_week
    return f


def models() -> dict:
    # Fixed, conservative settings. Shallow trees + strong regularisation
    # because daily direction is mostly noise; do not tune these on results.
    return {
        "gbm": HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.03, max_iter=200, min_samples_leaf=50,
            l2_regularization=1.0, random_state=SEED),
        "logit": make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000)),
    }


def walk_forward(X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
    out = []
    for year in sorted(set(X.index.year)):
        if year < FIRST_TEST_YEAR:
            continue
        train = X.index.year < year
        test = X.index.year == year
        row = pd.DataFrame(index=X.index[test])
        for name, m in models().items():
            m.fit(X[train], y[train])
            row[f"p_{name}"] = m.predict_proba(X[test])[:, 1]
        out.append(row)
    return pd.concat(out)


def backtest(pos: pd.Series, next_ret: pd.Series, fee: float) -> pd.Series:
    """Daily log-equity returns of a long/flat position, fees per change."""
    turns = pos.diff().abs().fillna(pos.iloc[0])
    simple = np.expm1(next_ret) * pos - turns * fee
    return np.log1p(simple)


def stats(logret: pd.Series) -> dict:
    eq = np.exp(logret.cumsum())
    years = len(logret) / 365
    dd = (eq / eq.cummax() - 1).min()
    sharpe = logret.mean() / logret.std() * np.sqrt(365) if logret.std() > 0 else 0.0
    return {"total_return_%": round((eq.iloc[-1] - 1) * 100, 1),
            "cagr_%": round((eq.iloc[-1] ** (1 / years) - 1) * 100, 1),
            "sharpe": round(float(sharpe), 2),
            "max_drawdown_%": round(float(dd) * 100, 1)}


def binom_p_greater(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), via log-space sum (no scipy needed)."""
    from math import exp, log
    total = 0.0
    for i in range(k, n + 1):
        total += exp(log(comb(n, i)) + i * log(p) + (n - i) * log(1 - p))
    return min(total, 1.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/btc_daily/BTC-USD.csv")
    ap.add_argument("--fee", type=float, default=0.0025, help="fee per side, fraction")
    ap.add_argument("--out", default="ml/output")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    df = load(a.csv)
    X = features(df)
    next_ret = np.log(df["Close"].shift(-1) / df["Close"])
    y = (next_ret > 0).astype(int)
    ok = X.notna().all(axis=1) & next_ret.notna()
    X, y, next_ret = X[ok], y[ok], next_ret[ok]
    print(f"Data: {df.index[0].date()} -> {df.index[-1].date()}, "
          f"{len(X)} usable days, {X.shape[1]} features")

    preds = walk_forward(X, y)
    yt, rt = y.loc[preds.index], next_ret.loc[preds.index]
    base_rate = float(yt.mean())

    report: dict = {"test_period": f"{preds.index[0].date()} -> {preds.index[-1].date()}",
                    "test_days": len(preds), "fee_per_side": a.fee,
                    "up_day_base_rate": round(base_rate, 4), "models": {}}

    curves = {"buy_and_hold": rt}
    report["buy_and_hold"] = stats(rt)
    for name in ("gbm", "logit"):
        p = preds[f"p_{name}"]
        hit = int(((p > 0.5).astype(int) == yt).sum())
        n = len(yt)
        # Compare against the best naive guess (always "up" if up-days dominate).
        naive = max(base_rate, 1 - base_rate)
        pos = (p > 0.5).astype(float)
        lr = backtest(pos, rt, a.fee)
        curves[name] = lr
        per_year = {str(yr): stats(lr[lr.index.year == yr])["total_return_%"]
                    for yr in sorted(set(lr.index.year))}
        report["models"][name] = {
            "accuracy": round(hit / n, 4),
            "naive_accuracy": round(naive, 4),
            "p_value_vs_naive": round(binom_p_greater(hit, n, naive), 4),
            "auc": round(float(roc_auc_score(yt, p)), 4),
            "days_in_market_%": round(float(pos.mean()) * 100, 1),
            "position_changes": int(pos.diff().abs().sum()),
            **stats(lr),
            "bh_return_by_year_%": {str(yr): stats(rt[rt.index.year == yr])["total_return_%"]
                                    for yr in sorted(set(rt.index.year))},
            "model_return_by_year_%": per_year,
        }

    # Sanity check: the same pipeline on shuffled labels should land at ~0.5
    # AUC. If it doesn't, something leaks.
    rng = np.random.default_rng(SEED)
    y_shuf = pd.Series(rng.permutation(y.values), index=y.index)
    shuf = walk_forward(X, y_shuf)
    report["leak_check_shuffled_auc_gbm"] = round(float(
        roc_auc_score(y_shuf.loc[shuf.index], shuf["p_gbm"])), 4)

    preds.assign(next_ret=rt, up=yt).to_csv(out / "walk_forward_predictions.csv")
    (out / "report.json").write_text(json.dumps(report, indent=2))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5))
        for k, lr in curves.items():
            ax.plot(np.exp(lr.cumsum()), label=k)
        ax.set_yscale("log")
        ax.set_title(f"Walk-forward equity, fee {a.fee*100:.2f}%/side (log scale)")
        ax.legend()
        ax.grid(alpha=.3)
        fig.tight_layout()
        fig.savefig(out / "equity.png", dpi=120)
    except Exception as exc:  # noqa: BLE001 - plotting is optional
        print("plot skipped:", exc)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
