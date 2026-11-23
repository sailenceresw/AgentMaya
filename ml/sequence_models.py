"""Sequence deep-learning models (1D CNN, LSTM) on raw hourly history.

Unlike the zoo (hand-made summary features), these read the last L hours of
raw per-hour channels and learn their own patterns. Same question, same
H=24 decisions, same costs, same locked 2026 holdout.

Rules fixed 2026-11-17, before torch was even installed (do not tune):
- Input: last L=96 hourly bars of: 1h log return, high-low range, volume
  z-score (168h), plus taker delta ratio on the perp dataset (real order
  flow; the long dataset has none). Channels standardised with training-set
  mean/std only.
- cnn:  3 dilated Conv1d layers (16 ch, kernel 5, dilation 1/2/4), ReLU,
        global average pool, dropout 0.2, linear.
- lstm: 1-layer LSTM, 32 hidden, last state, dropout 0.2, linear.
- Training: binary cross-entropy on "up over the next 24h", Adam lr 1e-3,
  weight decay 1e-4, batch 256, 8 epochs, keep the epoch with the best loss
  on the last 15% (by time) of the training window. Two seeds, probabilities
  averaged. Training samples every 6th hour (24h labels overlap heavily).
- Walk-forward retrains QUARTERLY (not monthly: CPU cost), expanding window,
  first 12 months train only, last H hours before each test block purged.
- Positions, fees, timing test as in model_zoo. These are tries 19-20, so
  "passes" means timing p < 0.05/20.

Run:  python ml/sequence_models.py --dataset perp
      python ml/sequence_models.py --dataset long --parts "C:/Users/<you>/Videos/btcusd_1-min_data_parts"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import roc_auc_score
from torch import nn

sys.path.insert(0, str(Path(__file__).parent))
import btc_hourly_orderflow as m  # noqa: E402
import model_zoo as zoo  # noqa: E402

H, L, STEP, EPOCHS, BATCH = 24, 96, 6, 8, 256
SEEDS = (7, 8)
N_TRIES = 20
torch.set_num_threads(4)


class CNN(nn.Module):
    def __init__(self, c: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(c, 16, 5, padding=2), nn.ReLU(),
            nn.Conv1d(16, 16, 5, padding=4, dilation=2), nn.ReLU(),
            nn.Conv1d(16, 16, 5, padding=8, dilation=4), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1), nn.Flatten(), nn.Dropout(0.2), nn.Linear(16, 1))

    def forward(self, x):  # x: (batch, L, channels)
        return self.net(x.transpose(1, 2)).squeeze(-1)


class LSTM(nn.Module):
    def __init__(self, c: int):
        super().__init__()
        self.rnn = nn.LSTM(c, 32, batch_first=True)
        self.head = nn.Sequential(nn.Dropout(0.2), nn.Linear(32, 1))

    def forward(self, x):
        o, _ = self.rnn(x)
        return self.head(o[:, -1]).squeeze(-1)


ARCH = {"cnn": CNN, "lstm": LSTM}


def fit_predict(arch, Xtr, ytr, Xte, seed: int) -> np.ndarray:
    torch.manual_seed(seed)
    np.random.seed(seed)
    cut = int(len(Xtr) * 0.85)  # validation = the most recent 15%, never shuffled in
    xt, yt = torch.from_numpy(Xtr[:cut]), torch.from_numpy(ytr[:cut])
    xv, yv = torch.from_numpy(Xtr[cut:]), torch.from_numpy(ytr[cut:])
    net = arch(Xtr.shape[2])
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    lossf = nn.BCEWithLogitsLoss()
    best, best_state = np.inf, None
    for _ in range(EPOCHS):
        net.train()
        perm = torch.randperm(len(xt))
        for i in range(0, len(xt), BATCH):
            b = perm[i:i + BATCH]
            opt.zero_grad()
            lossf(net(xt[b]), yt[b]).backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            v = float(lossf(net(xv), yv))
        if v < best:
            best, best_state = v, {k: t.clone() for k, t in net.state_dict().items()}
    net.load_state_dict(best_state)
    net.eval()
    with torch.no_grad():
        return torch.sigmoid(net(torch.from_numpy(Xte))).numpy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["perp", "long"], required=True)
    ap.add_argument("--parts")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--unlock-holdout", action="store_true")
    a = ap.parse_args()
    out = Path(f"ml/output_seq_{a.dataset}" + ("_holdout" if a.unlock_holdout else ""))
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    X, fwd, fund, ts, dec = zoo.build(a.dataset, a.parts, a.unlock_holdout)
    y = (fwd > 0).astype(np.float32)
    chans = ["ret_1h", "range_pct", "volume_z168"] + (["delta_ratio_1h"] if "delta_ratio_1h" in X else [])
    # Full hourly grid so a window of L rows is L consecutive hours; any
    # dropped row inside a window makes it NaN and the sample is skipped.
    lo = X.index.min()
    grid = X[chans].reindex(range(lo, X.index.max() + 1)).to_numpy(np.float32)
    win = np.lib.stride_tricks.sliding_window_view(grid, L, axis=0)  # (n-L+1, C, L)

    def windows(idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        p = idx - lo - L + 1
        okp = p >= 0
        w = np.full((len(idx), L, len(chans)), np.nan, np.float32)
        w[okp] = win[p[okp]].transpose(0, 2, 1)
        return w, ~np.isnan(w).any(axis=(1, 2))

    month = ts.dt.strftime("%Y-%m")
    months = sorted(month.unique())[m.TRAIN_MONTHS:]
    blocks = [months[i:i + 3] for i in range(0, len(months), 3)]
    sample = pd.Series((ts.dt.hour % STEP == 0).values, index=X.index)
    print(f"{a.dataset}: {len(X)} rows, channels {chans}, {len(blocks)} quarterly blocks, "
          f"holdout {'UNLOCKED' if a.unlock_holdout else 'locked'}", flush=True)

    def run(labels: pd.Series, seeds) -> pd.DataFrame:
        rows = []
        for bi, blk in enumerate(blocks):
            test = month.isin(blk) & dec
            first = test[test].index.min()
            if pd.isna(first):
                continue
            train = (month < blk[0]) & (X.index < first - H) & sample
            tr_idx, te_idx = X.index[train].to_numpy(), X.index[test].to_numpy()
            Wtr, oktr = windows(tr_idx)
            Wte, okte = windows(te_idx)
            Wtr, ytr = Wtr[oktr], labels.loc[tr_idx].to_numpy(np.float32)[oktr]
            mu, sd = Wtr.reshape(-1, len(chans)).mean(0), Wtr.reshape(-1, len(chans)).std(0) + 1e-8
            Wtr = (Wtr - mu) / sd
            Wte = np.nan_to_num((Wte - mu) / sd)
            row = pd.DataFrame(index=te_idx[okte])
            for name, arch in ARCH.items():
                ps = [fit_predict(arch, Wtr, ytr, Wte[okte], s) for s in seeds]
                row[f"p_{name}"] = np.mean(ps, axis=0)
            rows.append(row)
            print(f"  block {bi + 1}/{len(blocks)} {blk[0]} ({len(ytr)} train) "
                  f"{(time.time() - t0) / 60:.1f} min", flush=True)
        return pd.concat(rows)

    preds = run(pd.Series(y, index=X.index), SEEDS)
    rt, ft = np.expm1(fwd.loc[preds.index]), fund.loc[preds.index]
    yt = y.loc[preds.index].astype(int)
    is_hold = (ts.loc[preds.index] >= pd.Timestamp(m.HOLDOUT_START, tz="UTC")).values
    yr = ts.loc[preds.index].dt.year.values
    rng = np.random.default_rng(7)

    def score(sel: np.ndarray) -> dict:
        res: dict = {"decisions": int(sel.sum()),
                     "buy_and_hold": m.stats(rt[sel] - ft[sel], 365)}
        n1, n0 = int(yt[sel].sum()), int((1 - yt[sel]).sum())
        for name in ARCH:
            s = preds[f"p_{name}"][sel]
            pos = m.positions(s)
            r, cost = m.net_returns(pos, rt[sel], ft[sel], a.fee)
            auc = float(roc_auc_score(yt[sel], s))
            g = lambda q: np.expm1(np.log1p(q * rt[sel].values).sum())  # noqa: E731
            g_real = g(pos.values)
            null = np.array([g(rng.permutation(pos.values)) for _ in range(5000)])
            p_t = (1 + (null >= g_real).sum()) / 5001
            res[name] = {"auc": round(auc, 4), "auc_p": round(zoo.auc_p(auc, n1, n0), 4),
                         "traded_%": round(float((pos != 0).mean()) * 100, 1),
                         "gross_%": round(float(g_real) * 100, 1), "timing_p": round(float(p_t), 4),
                         "passes_corrected": bool(p_t < 0.05 / N_TRIES),
                         "fees_%": round(float(cost.sum()) * 100, 1), **m.stats(r, 365),
                         "by_year_%": {str(v): m.stats(r[yr[sel] == v], 365)["total_return_%"]
                                       for v in sorted(set(yr[sel]))}}
        return res

    report = {"dataset": a.dataset, "channels": chans, "lookback_h": L,
              "development": score(~is_hold)}
    if is_hold.any():
        report["holdout"] = score(is_hold)
    print("shuffled-label control (1 seed)...", flush=True)
    y_s = pd.Series(np.random.default_rng(7).permutation(y.values), index=X.index)
    sh = run(y_s, SEEDS[:1])
    report["shuffled_label_auc"] = {n: round(float(roc_auc_score(
        y_s.loc[sh.index].astype(int), sh[f"p_{n}"])), 4) for n in ARCH}
    report["minutes"] = round((time.time() - t0) / 60, 1)
    preds.assign(fwd_ret=rt, funding=ft, up=yt).to_csv(out / "walk_forward_predictions.csv")
    (out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
