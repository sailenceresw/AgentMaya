# %% [markdown]
# # Fine-tune Kronos-small on BTC 15m bars, then test it honestly
#
# Separate from the ``maya`` package. Fine-tunes the pretrained Kronos-small
# predictor (github.com/shiyu-coder/Kronos, MIT) on BTC/USD 15-minute bars and
# compares it with the untouched pretrained model on a held-out period.
#
# Question: does fine-tuning on BTC make Kronos's 1-hour-ahead (4 x 15m)
# forecast good enough to pay fees, and is it better than the pretrained
# model that ``maya/signals/kronos_signal.py`` uses today?
#
# Rules fixed before looking at any result (do not tune them on the output):
# - Three time-ordered splits taken from the end of the data:
#   test = last TEST_DAYS, val = VAL_DAYS before that, train = START..val.
#   Every window lies wholly inside one split, so no window crosses a boundary.
# - Each window is normalised with its lookback bars only (as Kronos's own
#   finetune/dataset.py does and as KronosPredictor does at inference).
# - Only the predictor is fine-tuned; the tokenizer stays pretrained, so the
#   saved model is a drop-in replacement for NeoQuasar/Kronos-small.
# - Validation loss is the ONLY thing used to choose anything (the checkpoint).
# - The test split is scored once, at the end, for both models with the same
#   seed and settings. If you change settings after seeing test numbers and
#   rerun, those new test numbers are no longer out-of-sample.
# - Trading rule and costs are maya's: long/short only when the forecast move
#   beats round trip + margin (kronos_signal.signal_rule), 0.10% round trip
#   (Binance USD-M taker both sides) + 5 bps margin, hold pred_len bars.
#
# Data: Kaggle "Bitcoin Historical Data" (btcusd_1-min_data.csv, Bitstamp spot,
# UTC unix-second timestamps). Volume scale differs from Binance perps, but
# every window is z-normalised so only the shape matters.
#
# Kaggle: Accelerator = GPU T4, Internet = On, add the dataset as an input,
# then "Save Version -> Save & Run All". Outputs land in /kaggle/working.
# Local smoke test (CPU, minutes):
#   python ml/kronos_finetune.py --smoke --data <csv> --kronos-repo <clone>

# %%
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ON_KAGGLE = os.path.isdir("/kaggle/input")


@dataclass
class Cfg:
    data: str = ""                     # path to btcusd_1-min_data.csv ('' = search /kaggle/input)
    kronos_repo: str = "/tmp/Kronos" if ON_KAGGLE else ""
    out: str = "/kaggle/working/kronos_out" if ON_KAGGLE else "ml/output_kronos"
    model_id: str = "NeoQuasar/Kronos-small"
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    start: str = "2018-01-01"          # earlier Bitstamp data is thin and gappy
    bar: str = "15min"
    test_days: int = 180
    val_days: int = 120
    lookback: int = 256                # maya's KronosForecaster default
    pred_len: int = 4                  # maya's default: 4 bars = 1 hour
    clip: float = 5.0
    # Training (lr/betas/wd/clip follow Kronos finetune/config.py)
    steps: int = 8000
    batch: int = 32
    lr: float = 4e-5
    weight_decay: float = 0.1
    grad_clip: float = 3.0
    eval_every: int = 400
    patience: int = 5                  # evals without val improvement before stopping
    val_windows: int = 2048
    test_windows: int = 2048
    amp: bool = True
    # Forecast evaluation (maya defaults: samples=3, T=1.0, top_p=0.9)
    samples: int = 3
    temperature: float = 1.0
    top_p: float = 0.9
    round_trip: float = 0.001
    margin: float = 0.0005
    max_decisions: int = 0             # 0 = every non-overlapping test decision
    predict_batch: int = 256
    seed: int = 7
    smoke: bool = False


def parse_cfg() -> Cfg:
    ap = argparse.ArgumentParser()
    for k, v in asdict(Cfg()).items():
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            ap.add_argument(flag, action="store_true", default=v)
        else:
            ap.add_argument(flag, type=type(v), default=v)
    # parse_known_args: Jupyter passes its own -f argument.
    a, _ = ap.parse_known_args()
    c = Cfg(**vars(a))
    if c.smoke:
        # Proves the pipeline end to end on CPU; the numbers mean nothing.
        c.steps, c.batch, c.eval_every, c.patience = 6, 4, 3, 2
        c.val_windows = c.test_windows = 16
        c.max_decisions, c.predict_batch, c.amp = 8, 8, False
        if c.out == Cfg.out:
            c.out = c.out + "_smoke"
    return c


# %% [markdown]
# ## Setup: Kronos code (not on PyPI) and the dataset

# %%
def ensure_kronos(c: Cfg) -> None:
    if ON_KAGGLE and not os.path.isdir(os.path.join(c.kronos_repo, "model")):
        subprocess.run(["git", "clone", "--depth", "1",
                        "https://github.com/shiyu-coder/Kronos", c.kronos_repo], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "einops", "huggingface_hub", "safetensors"], check=True)
    if not os.path.isdir(os.path.join(c.kronos_repo, "model")):
        raise SystemExit(f"--kronos-repo {c.kronos_repo!r} is not a Kronos clone (no model/ folder)")
    if c.kronos_repo not in sys.path:
        sys.path.insert(0, c.kronos_repo)


def find_data(c: Cfg) -> str:
    if c.data:
        return c.data
    hits = glob.glob("/kaggle/input/**/btcusd_1-min_data.csv", recursive=True)
    if not hits:
        raise SystemExit("btcusd_1-min_data.csv not found: add the Kaggle dataset "
                         "'Bitcoin Historical Data' (mczielinski) as an input, or pass --data")
    return hits[0]


# %% [markdown]
# ## Data: 1-minute -> 15-minute bars, contiguous windows, time splits

# %%
FEATS = ["open", "high", "low", "close", "volume", "amount"]


def load_bars(path: str, c: Cfg) -> pd.DataFrame:
    raw = pd.read_csv(path)
    raw["ts"] = pd.to_datetime(raw["Timestamp"].astype("int64"), unit="s", utc=True)
    raw = raw.set_index("ts").sort_index()
    raw = raw[~raw.index.duplicated()]
    raw = raw[raw.index >= pd.Timestamp(c.start, tz="UTC")]
    r = raw.resample(c.bar, label="left", closed="left")
    bars = pd.DataFrame({"open": r["Open"].first(), "high": r["High"].max(),
                         "low": r["Low"].min(), "close": r["Close"].last(),
                         "volume": r["Volume"].sum(), "n": r["Close"].count()})
    minutes = int(pd.Timedelta(c.bar) / pd.Timedelta("1min"))
    # Keep only complete bars: a bar missing minutes (exchange outage, or the
    # still-forming last bar) would be a different object from what maya sees.
    bars = bars[bars["n"] == minutes].drop(columns="n")
    # Same derivation KronosPredictor uses when no 'amount' is passed, which is
    # what maya does -- so training and inference see the same 6th feature.
    bars["amount"] = bars["volume"] * bars[["open", "high", "low", "close"]].mean(axis=1)
    return bars


def stamps(idx: pd.DatetimeIndex) -> np.ndarray:
    # Column order must match model.kronos.calc_time_stamps.
    return np.stack([idx.minute, idx.hour, idx.day_of_week, idx.day, idx.month], axis=1).astype(np.float32)


def valid_starts(idx: pd.DatetimeIndex, lo: pd.Timestamp, hi: pd.Timestamp, w: int,
                 bar: pd.Timedelta) -> np.ndarray:
    """Start positions of w-bar windows that are gap-free and lie in [lo, hi)."""
    t = idx.values
    gap = np.diff(t) != bar.to_timedelta64()
    seg = np.concatenate([[0], np.cumsum(gap)])
    s = np.arange(len(t) - w + 1)
    ok = (seg[s] == seg[s + w - 1]) & (idx[s] >= lo) & (idx[s + w - 1] < hi)
    return s[ok]


def even(a: np.ndarray, n: int) -> np.ndarray:
    if n <= 0 or len(a) <= n:
        return a
    return a[np.linspace(0, len(a) - 1, n).astype(int)]


# %% [markdown]
# ## Training and evaluation helpers

# %%
def build(c: Cfg):
    import torch
    from model import Kronos, KronosTokenizer  # type: ignore[import-not-found]
    tok = KronosTokenizer.from_pretrained(c.tokenizer_id)
    mdl = Kronos.from_pretrained(c.model_id)
    return tok, mdl, torch


class Windows:
    """Torch dataset of normalised windows starting at the given positions."""

    def __init__(self, x: np.ndarray, st: np.ndarray, starts: np.ndarray, c: Cfg) -> None:
        self.x, self.st, self.starts, self.c = x, st, starts, c
        self.w = c.lookback + c.pred_len + 1

    def __len__(self) -> int:
        return len(self.starts)

    def __getitem__(self, i: int):
        import torch
        s = int(self.starts[i])
        x = self.x[s:s + self.w]
        past = x[:self.c.lookback]
        x = (x - past.mean(axis=0)) / (past.std(axis=0) + 1e-5)
        x = np.clip(x, -self.c.clip, self.c.clip).astype(np.float32)
        return torch.from_numpy(x), torch.from_numpy(self.st[s:s + self.w])


def batch_loss(model, tok, x, stamp, teacher_forcing: bool):
    import torch
    # Tokenise in fp32 even under AMP: inference (KronosPredictor) does, and
    # the binary quantiser's token ids should not depend on training precision.
    with torch.no_grad(), torch.autocast(device_type=x.device.type, enabled=False):
        s1, s2 = tok.encode(x.float(), half=True)
    s1_in, s2_in, s1_out, s2_out = s1[:, :-1], s2[:, :-1], s1[:, 1:], s2[:, 1:]
    if teacher_forcing:
        logits = model(s1_in, s2_in, stamp[:, :-1], use_teacher_forcing=True, s1_targets=s1_out)
    else:
        # As Kronos's train_predictor.py: s2 is conditioned on a sampled s1.
        logits = model(s1_in, s2_in, stamp[:, :-1])
    return model.head.compute_loss(logits[0], logits[1], s1_out, s2_out)


def eval_loss(model, tok, loader, device, torch) -> float:
    # Teacher forcing makes this the exact NLL of the data under the model,
    # so it is deterministic and comparable between checkpoints and models.
    model.eval()
    tot, n = 0.0, 0
    with torch.no_grad():
        for x, st in loader:
            x, st = x.to(device), st.to(device)
            loss, _, _ = batch_loss(model, tok, x, st, teacher_forcing=True)
            tot += loss.item() * len(x)
            n += len(x)
    return tot / max(n, 1)


def train(model, tok, train_ds, val_loader, c: Cfg, device, torch, out: Path) -> dict:
    from torch.utils.data import DataLoader, RandomSampler
    g = torch.Generator().manual_seed(c.seed)
    sampler = RandomSampler(train_ds, replacement=True, num_samples=c.steps * c.batch, generator=g)
    loader = DataLoader(train_ds, batch_size=c.batch, sampler=sampler, drop_last=True,
                        num_workers=2 if device.type == "cuda" else 0,
                        pin_memory=device.type == "cuda")
    opt = torch.optim.AdamW(model.parameters(), lr=c.lr, betas=(0.9, 0.95), weight_decay=c.weight_decay)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=c.lr, total_steps=c.steps,
                                                pct_start=0.03, div_factor=10)
    use_amp = c.amp and device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)

    history = [{"step": 0, "val_loss": eval_loss(model, tok, val_loader, device, torch)}]
    best, best_step, bad = history[0]["val_loss"], 0, 0
    best_dir = out / "kronos-small-btc15m"
    model.save_pretrained(best_dir)  # step 0 == pretrained, so "best" is never worse than it
    print(f"step 0 (pretrained)  val_loss {best:.4f}")

    t0, run = time.time(), 0.0
    model.train()
    for step, (x, st) in enumerate(loader, start=1):
        x, st = x.to(device, non_blocking=True), st.to(device, non_blocking=True)
        with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
            loss, _, _ = batch_loss(model, tok, x, st, teacher_forcing=False)
        opt.zero_grad(set_to_none=True)
        scaler.scale(loss).backward()
        scaler.unscale_(opt)
        torch.nn.utils.clip_grad_norm_(model.parameters(), c.grad_clip)
        scaler.step(opt)
        scaler.update()
        sched.step()
        run = 0.98 * run + 0.02 * loss.item() if step > 1 else loss.item()

        if step % c.eval_every == 0 or step == c.steps:
            v = eval_loss(model, tok, val_loader, device, torch)
            history.append({"step": step, "train_loss_ema": run, "val_loss": v})
            mark = ""
            if v < best:
                best, best_step, bad, mark = v, step, 0, "  <- best, saved"
                model.save_pretrained(best_dir)
            else:
                bad += 1
            print(f"step {step:>6}  train~{run:.4f}  val_loss {v:.4f}  "
                  f"{(time.time() - t0) / 60:.1f} min{mark}")
            model.train()
            if bad >= c.patience:
                print(f"early stop: no val improvement in {c.patience} evals")
                break
    return {"history": history, "best_val_loss": best, "best_step": best_step,
            "pretrained_val_loss": history[0]["val_loss"], "saved_to": str(best_dir)}


# %% [markdown]
# ## Forecast test: maya's signal rule, fees included

# %%
def forecasts(predictor, bars: pd.DataFrame, starts: np.ndarray, c: Cfg, torch) -> np.ndarray:
    """Forecast close pred_len bars after each context window."""
    torch.manual_seed(c.seed)  # both models draw the same sampling stream
    idx = bars.index.tz_convert(None)
    vals = bars[FEATS].values
    step = pd.Timedelta(c.bar)
    out = []
    for b in range(0, len(starts), c.predict_batch):
        dfs, xts, yts = [], [], []
        for s in starts[b:b + c.predict_batch]:
            s = int(s)
            dfs.append(pd.DataFrame(vals[s:s + c.lookback], columns=FEATS))
            xts.append(pd.Series(idx[s:s + c.lookback]))
            last = idx[s + c.lookback - 1]
            yts.append(pd.Series([last + step * (i + 1) for i in range(c.pred_len)]))
        preds = predictor.predict_batch(dfs, xts, yts, pred_len=c.pred_len, T=c.temperature,
                                        top_p=c.top_p, sample_count=c.samples, verbose=False)
        out.extend(float(p["close"].iloc[-1]) for p in preds)
    return np.array(out)


def trade_stats(pred: np.ndarray, last: np.ndarray, real: np.ndarray, c: Cfg) -> dict:
    pr, rr = pred / last - 1, real / last - 1
    band = c.round_trip + c.margin
    pos = np.where(pr > band, 1.0, np.where(pr < -band, -1.0, 0.0))
    r = pos * rr - np.abs(pos) * c.round_trip
    traded = pos != 0
    moved = rr != 0
    hit = float((np.sign(pr[moved]) == np.sign(rr[moved])).mean())
    n = int(moved.sum())
    # One-sided p-value that hit rate > 50% (normal approx to the binomial).
    p_hit = 0.5 * math.erfc((hit - 0.5) / math.sqrt(0.25 / max(n, 1)) / math.sqrt(2))
    per_year = 365 * 24 * 60 / (pd.Timedelta(c.bar).total_seconds() / 60) / c.pred_len
    eq = np.cumprod(1 + r)
    sharpe = r.mean() / r.std() * math.sqrt(per_year) if r.std() > 0 else 0.0
    return {
        "decisions": len(r),
        "direction_hit_rate": round(hit, 4),
        "p_value_hit_gt_50%": round(p_hit, 4),
        # Spearman via ranks + Pearson, so scipy is not required.
        "rank_ic": round(float(np.corrcoef(pd.Series(pr).rank(), pd.Series(rr).rank())[0, 1]), 4),
        "traded_%": round(float(traded.mean()) * 100, 1),
        "hit_rate_when_traded": round(float((np.sign(pos[traded]) == np.sign(rr[traded])).mean()), 4)
        if traded.any() else None,
        "fees_paid_%": round(float(np.abs(pos).sum() * c.round_trip) * 100, 1),
        "total_return_%": round(float(eq[-1] - 1) * 100, 1),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_%": round(float((eq / np.maximum.accumulate(eq) - 1).min()) * 100, 1),
    }, r


# %% [markdown]
# ## Run

# %%
def main() -> None:
    c = parse_cfg()
    out = Path(c.out)
    out.mkdir(parents=True, exist_ok=True)
    ensure_kronos(c)
    tok, model, torch = build(c)
    from model import Kronos, KronosPredictor  # type: ignore[import-not-found]
    torch.manual_seed(c.seed)
    np.random.seed(c.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("device:", device, torch.cuda.get_device_name(0) if device.type == "cuda" else "")
    tok = tok.to(device).eval()
    for p in tok.parameters():
        p.requires_grad_(False)
    model = model.to(device)

    path = find_data(c)
    bars = load_bars(path, c)
    end = bars.index[-1] + pd.Timedelta(c.bar)
    test_lo = end - pd.Timedelta(days=c.test_days)
    val_lo = test_lo - pd.Timedelta(days=c.val_days)
    bar = pd.Timedelta(c.bar)
    w = c.lookback + c.pred_len + 1
    tr = valid_starts(bars.index, bars.index[0], val_lo, w, bar)
    va = even(valid_starts(bars.index, val_lo, test_lo, w, bar), c.val_windows)
    te = even(valid_starts(bars.index, test_lo, end, w, bar), c.test_windows)
    splits = {"train": f"{bars.index[0]} -> {val_lo}", "val": f"{val_lo} -> {test_lo}",
              "test": f"{test_lo} -> {end}"}
    print(f"{len(bars)} complete {c.bar} bars from {path}")
    for k, v in splits.items():
        print(f"  {k:5} {v}")
    print(f"  windows: train {len(tr)}, val {len(va)}, test {len(te)}")
    if len(tr) == 0 or len(va) == 0 or len(te) == 0:
        raise SystemExit("a split has no complete windows; check --start / --val-days / --test-days")

    x = bars[FEATS].values.astype(np.float64)
    st = stamps(bars.index)
    from torch.utils.data import DataLoader
    vl = DataLoader(Windows(x, st, va, c), batch_size=max(c.batch, 64))
    tl = DataLoader(Windows(x, st, te, c), batch_size=max(c.batch, 64))

    fit = train(model, tok, Windows(x, st, tr, c), vl, c, device, torch, out)

    # ---- Test split: scored once, both models, same windows and seed ----
    pre = Kronos.from_pretrained(c.model_id).to(device)
    best = Kronos.from_pretrained(fit["saved_to"]).to(device)
    nll = {"pretrained": eval_loss(pre, tok, tl, device, torch),
           "finetuned": eval_loss(best, tok, tl, device, torch)}
    print(f"test NLL  pretrained {nll['pretrained']:.4f}  finetuned {nll['finetuned']:.4f}")

    dec = valid_starts(bars.index, test_lo, end, c.lookback + c.pred_len, bar)
    keep, last_end = [], -1
    for s in dec:  # non-overlapping decisions: each hold ends before the next starts
        if s + c.lookback - 1 >= last_end:
            keep.append(s)
            last_end = s + c.lookback - 1 + c.pred_len
    dec = even(np.array(keep), c.max_decisions)
    close = bars["close"].values
    last = close[dec + c.lookback - 1]
    real = close[dec + c.lookback - 1 + c.pred_len]
    print(f"forecasting {len(dec)} test decisions x 2 models ...")

    report = {"config": asdict(c), "data": path, "splits": splits,
              "windows": {"train": len(tr), "val": len(va), "test": len(te)},
              "training": {k: v for k, v in fit.items() if k != "history"},
              "test_nll": {k: round(v, 4) for k, v in nll.items()}, "test_trading": {}}
    curves = {}
    for name, m in (("pretrained", pre), ("finetuned", best)):
        pred = forecasts(KronosPredictor(m, tok, device=str(device), max_context=512, clip=c.clip),
                         bars, dec, c, torch)
        report["test_trading"][name], curves[name] = trade_stats(pred, last, real, c)
    hold = real / last - 1
    eq = np.cumprod(1 + hold)
    report["test_trading"]["long_hold_no_fees"] = {
        "total_return_%": round(float(eq[-1] - 1) * 100, 1),
        "max_drawdown_%": round(float((eq / np.maximum.accumulate(eq) - 1).min()) * 100, 1)}
    curves["long hold"] = hold

    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    pd.DataFrame(fit["history"]).to_csv(out / "val_history.csv", index=False)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5))
        h = pd.DataFrame(fit["history"])
        a1.plot(h["step"], h["val_loss"], marker="o", label="val NLL")
        if "train_loss_ema" in h:
            a1.plot(h["step"], h["train_loss_ema"], alpha=.6, label="train loss (EMA)")
        a1.set_title("Fine-tuning (step 0 = pretrained)")
        a1.set_xlabel("step")
        a1.legend()
        a1.grid(alpha=.3)
        when = bars.index[dec + c.lookback - 1]
        for k, r in curves.items():
            a2.plot(when, np.cumprod(1 + r), label=k)
        a2.set_title(f"Test equity, {c.pred_len}x{c.bar} hold, {c.round_trip * 100:.2f}% round trip")
        a2.legend()
        a2.grid(alpha=.3)
        fig.tight_layout()
        fig.savefig(out / "results.png", dpi=120)
    except Exception as exc:  # noqa: BLE001 - plotting is optional
        print("plot skipped:", exc)
    print(json.dumps({k: report[k] for k in ("splits", "training", "test_nll", "test_trading")},
                     indent=2, default=str))
    print(f"\nSaved: {out}/report.json, results.png, kronos-small-btc15m/ (the fine-tuned weights)")


if __name__ == "__main__":
    main()
