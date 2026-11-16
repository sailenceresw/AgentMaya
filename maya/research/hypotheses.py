"""Catalogue of pre-declared hypotheses.

Each entry is one fixed configuration. To try a different parameter, register
a new id; editing an entry after seeing its result is tuning, and the registry
row for the old result stays either way.
"""
from __future__ import annotations

from maya.data.models import Candle
from maya.research.costs import Sample, SampleList
from maya.research.harness import Hypothesis
from maya.signals.indicators import delta_for


def forward_return(candles: list[Candle], i: int, horizon: int) -> float | None:
    """Close-to-close return from bar ``i`` to ``i + horizon``, or None.

    Entry is at the close of bar ``i``, the first moment a signal computed
    from that bar is known.
    """
    j = i + horizon
    if j >= len(candles) or candles[i].close <= 0:
        return None
    return candles[j].close / candles[i].close - 1.0


def _cvd_follow(data: dict[str, list[Candle]]) -> list[Sample]:
    """Trade with the bar's delta and hold one bar. This is the reading the
    legacy setups assume; the README measured it wrong-signed, so it is here
    as the reference a new idea has to beat, not as a candidate."""
    out: list[Sample] = []
    for candles in data.values():
        for i, c in enumerate(candles):
            d = delta_for(c)
            fwd = forward_return(candles, i, 1)
            if fwd is None or d == 0:
                continue
            out.append(Sample(c.timestamp, fwd if d > 0 else -fwd))
    return out


CATALOG: dict[str, Hypothesis] = {}


def register(hyp: Hypothesis) -> Hypothesis:
    if hyp.id in CATALOG:
        raise ValueError(f"hypothesis id {hyp.id!r} is already declared")
    CATALOG[hyp.id] = hyp
    return hyp


register(Hypothesis(
    id="cvd_follow_baseline",
    description="Reference: trade in the direction of each bar's delta, hold one bar, taker costs.",
    generate=_cvd_follow,
))


# -- pipeline hypotheses ----------------------------------------------------- #
def pipeline_samples(data: dict[str, list[Candle]], timeframe: str = "1m",
                     **overrides) -> list[Sample]:
    """Run the real graph over ``data`` and return its trades as samples.

    This is the backtester's own loop (same nodes, same risk layer), so a
    hypothesis about the pipeline tests the pipeline. Gross return is taken
    per trade from the journal, before fees; the harness charges its own cost
    model so every hypothesis is costed the same way.

    The lessons veto is switched off: it depends on the order trades happen
    in, which would make one hypothesis's result depend on its own history.
    """
    import os
    import sqlite3
    import tempfile

    from maya.backtest.runner import run_backtest
    from maya.config import get_settings

    base = get_settings()
    out = SampleList()
    for symbol, candles in data.items():
        settings = base.model_copy(update={
            "symbol": symbol, "timeframe": timeframe, "mode": base.mode.SIM,
            "llm_enabled": False, "lessons_veto_enabled": False, **overrides})
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            metrics = run_backtest(settings, iter(candles), journal_path=path,
                                   progress_every=0)
            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT timestamp_entry, pnl_usdt, fees_usdt, position_size, entry_price "
                "FROM trades").fetchall()
            conn.close()
        finally:
            os.unlink(path)
        for ts, pnl, fees, size, entry in rows:
            notional = (size or 0.0) * (entry or 0.0)
            if notional > 0:
                out.append(Sample(int(ts), (pnl + (fees or 0.0)) / notional))
        if metrics.maker:
            m = metrics.maker
            out.notes.append(
                f"{symbol} post-only: {m['placed']} placed, {m['filled']} filled, "
                f"{m['missed']} missed (missed share {m['missed_share']}); forward "
                f"{m['filled_forward_bps']} bps filled vs {m['missed_forward_bps']} bps "
                f"missed, adverse selection {m['adverse_selection_bps']} bps")
    return out


register(Hypothesis(
    id="baseline_1m_setups",
    description="Reference: the existing 1m setups as shipped, no multi-timeframe filter.",
    generate=lambda data: pipeline_samples(data),
    timeframe="1m",
))

register(Hypothesis(
    id="mtf_bias_filter",
    description="Existing 1m setups, taken only in the direction of the 15m bias "
                "(session VWAP side plus swing structure).",
    generate=lambda data: pipeline_samples(data, mtf_bias_filter_enabled=True),
    timeframe="1m",
))

register(Hypothesis(
    id="mtf_15s_trigger",
    description="15m bias, 1m setup and a 15s order-flow trigger together.",
    generate=lambda data: pipeline_samples(
        data, mtf_bias_filter_enabled=True, mtf_15s_trigger_enabled=True),
    timeframe="1m",
    blocked="needs trade-level (aggTrade) history; the kline cache has no ticks, "
            "and 15s bars are not faked from 1m candles",
))


register(Hypothesis(
    id="cvd_flip",
    description="Liquidity sweep and compression breakout with the CVD confirmation "
                "reversed, on 15m bars, nothing else enabled.",
    generate=lambda data: pipeline_samples(
        data, timeframe="15m", cvd_flip_setups_enabled=True,
        setups_only="liquidity_sweep_choch_cvd_flip,compression_breakout_cvd_flip"),
    timeframe="15m",
))


register(Hypothesis(
    id="maker_cvd_reversion",
    description="The cvd_flip setups entered with simulated post-only orders "
                "(60% fill when price trades through, one-candle wait), maker costs.",
    generate=lambda data: pipeline_samples(
        data, timeframe="15m", cvd_flip_setups_enabled=True,
        setups_only="liquidity_sweep_choch_cvd_flip,compression_breakout_cvd_flip",
        paper_entry_mode="post_only"),
    cost="maker_filled",
    timeframe="15m",
))
