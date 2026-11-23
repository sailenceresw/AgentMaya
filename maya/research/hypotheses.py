"""Catalogue of pre-declared hypotheses.

Each entry is one fixed configuration. To try a different parameter, register
a new id; editing an entry after seeing its result is tuning, and the registry
row for the old result stays either way.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
    for symbol, candles in data.items():
        for i, c in enumerate(candles):
            d = delta_for(c)
            fwd = forward_return(candles, i, 1)
            if fwd is None or d == 0:
                continue
            side = 1 if d > 0 else -1
            out.append(Sample(c.timestamp, side * fwd, symbol, side,
                              candles[i + 1].timestamp - c.timestamp))
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
    rows, notes = pipeline_trades(data, timeframe, **overrides)
    out = SampleList()
    for r in rows:
        if r["gross_return"] is not None:
            hold = max(0, int((r["timestamp_exit"] or r["timestamp_entry"]) - r["timestamp_entry"]))
            out.append(Sample(r["timestamp_entry"], r["gross_return"], r["data_symbol"],
                              1 if r["direction"] == "long" else -1, hold))
    out.notes.extend(notes)
    return out


# Every hypothesis that runs the pipeline, with its fixed settings, in one
# place so the diagnostics (maya.research.diagnose) replay exactly what was
# tested. Changing an entry after its result is tuning: declare a new id.
PIPELINE_CONFIGS: dict[str, tuple[str, dict]] = {
    "baseline_1m_setups": ("1m", {}),
    "mtf_bias_filter": ("1m", {"mtf_bias_filter_enabled": True}),
    "mtf_15s_trigger": ("1m", {"mtf_bias_filter_enabled": True,
                               "mtf_15s_trigger_enabled": True}),
    "cvd_flip": ("15m", {"cvd_flip_setups_enabled": True,
                         "setups_only": "liquidity_sweep_choch_cvd_flip,"
                                        "compression_breakout_cvd_flip"}),
    "maker_cvd_reversion": ("15m", {"cvd_flip_setups_enabled": True,
                                    "setups_only": "liquidity_sweep_choch_cvd_flip,"
                                                   "compression_breakout_cvd_flip",
                                    "paper_entry_mode": "post_only"}),
    "trail_1r_after_2r": ("1m", {"profit_trail_mode": "r", "profit_trail_r": 1.0,
                                 "profit_trail_start_r": 2.0}),
    # Declared 2026-11-18, before any 1h run: the baseline's diagnosis found
    # stops of median 12.5 bps against a 10 bps round trip, so fees ate most
    # of the risk. 1h bars give wider stops at the same fee. Shipped rules
    # unchanged, including the 4h hold limit (up to 4 bars here). Higher
    # timeframe context follows htf_list: 4h and 16h for 1h entries.
    "baseline_1h_setups": ("1h", {}),
    # Declared 2026-11-18 after baseline_1h_setups failed and its diagnosis
    # (exploration, not a test) showed 11 of 24 trades force-closed at 4h,
    # 82% of them winning. That look is why this exists, so it must clear the
    # full gate on its own. The only rule changed is the hold limit, 4h to
    # 48h, inside this backtest; live keeps 4h. Funding is charged at 1 bps
    # per 8h against every trade (Binance's base rate, assumed always paid).
    "baseline_1h_48h_hold": ("1h", {"max_hold_hours": 48, "funding_bps_per_8h": 1.0}),
}

# The scanner's universe as it stood on 2026-11-18 (15 coins, $5M+ volume),
# as Binance USD-M symbols. Chosen by today's volume, so it leans towards
# coins that did well; the holdout is the check on that. Coins listed after
# the start of the data contribute from their listing date.
SCANNER_15 = ("BTCUSDT,ETHUSDT,HYPEUSDT,ZECUSDT,SOLUSDT,XRPUSDT,NEARUSDT,PUMPUSDT,"
              "AAVEUSDT,1000PEPEUSDT,ENAUSDT,UNIUSDT,ADAUSDT,DOGEUSDT,BNBUSDT")


def _pipeline(hid: str):
    tf, overrides = PIPELINE_CONFIGS[hid]
    return lambda data: pipeline_samples(data, timeframe=tf, **overrides)


def pipeline_trades(data: dict[str, list[Candle]], timeframe: str = "1m",
                    **overrides) -> tuple[list[dict], list[str]]:
    """Run the real graph and return every journaled trade in full, plus notes.

    Each row is the journal's trade row with ``gross_return`` added: the
    return on notional before fees, which is what the harness costs.

    Two override keys are research-only and never reach Settings:
    ``max_hold_hours`` replaces the hard hold limit inside this backtest, and
    ``funding_bps_per_8h`` charges funding against every trade, whatever its
    side, for each started 8 hours held (the backtest itself charges none).
    """
    import math
    import os
    import sqlite3
    import tempfile

    from maya.backtest.runner import run_backtest
    from maya.config import get_settings

    max_hold = overrides.pop("max_hold_hours", None)
    funding_bps = overrides.pop("funding_bps_per_8h", 0.0)
    base = get_settings()
    trades: list[dict] = []
    notes: list[str] = []
    for symbol, candles in data.items():
        settings = base.model_copy(update={
            "symbol": symbol, "timeframe": timeframe, "mode": base.mode.SIM,
            "llm_enabled": False, "lessons_veto_enabled": False, **overrides})
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            metrics = run_backtest(settings, iter(candles), journal_path=path,
                                   progress_every=0, max_hold_hours=max_hold)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM trades ORDER BY timestamp_entry")]
            conn.close()
        finally:
            os.unlink(path)
        for r in rows:
            notional = (r["position_size"] or 0.0) * (r["entry_price"] or 0.0)
            r["symbol"] = r.get("symbol") or symbol
            # The key the candles came under, which the buy-and-hold gate
            # looks drift up by (the journal may spell the symbol differently).
            r["data_symbol"] = symbol
            r["gross_return"] = ((r["pnl_usdt"] + (r["fees_usdt"] or 0.0)) / notional
                                 if notional > 0 else None)
            if funding_bps and r["gross_return"] is not None:
                held_h = max(0.0, ((r["timestamp_exit"] or r["timestamp_entry"])
                                   - r["timestamp_entry"]) / 3_600_000)
                r["gross_return"] -= math.ceil(held_h / 8) * funding_bps / 10_000
            r["timestamp_entry"] = int(r["timestamp_entry"])
            trades.append(r)
        if metrics.maker:
            m = metrics.maker
            notes.append(
                f"{symbol} post-only: {m['placed']} placed, {m['filled']} filled, "
                f"{m['missed']} missed (missed share {m['missed_share']}); forward "
                f"{m['filled_forward_bps']} bps filled vs {m['missed_forward_bps']} bps "
                f"missed, adverse selection {m['adverse_selection_bps']} bps")
    return trades, notes


register(Hypothesis(
    id="baseline_1m_setups",
    description="Reference: the existing 1m setups as shipped, no multi-timeframe filter.",
    generate=_pipeline("baseline_1m_setups"),
    timeframe="1m",
))

register(Hypothesis(
    id="baseline_1h_setups",
    description="The shipped setups on 1h bars instead of 1m: wider stops at the same "
                "fee. Everything else as shipped, including the 4h hold limit.",
    generate=_pipeline("baseline_1h_setups"),
    timeframe="1h",
))

register(Hypothesis(
    id="baseline_1h_48h_hold",
    description="The shipped setups on 1h bars with a 48h hold limit instead of 4h "
                "(backtest only), funding charged, on the 15 scanner coins.",
    generate=_pipeline("baseline_1h_48h_hold"),
    timeframe="1h",
    symbols=SCANNER_15,
))

register(Hypothesis(
    id="mtf_bias_filter",
    description="Existing 1m setups, taken only in the direction of the 15m bias "
                "(session VWAP side plus swing structure).",
    generate=_pipeline("mtf_bias_filter"),
    timeframe="1m",
))

register(Hypothesis(
    id="mtf_15s_trigger",
    description="15m bias, 1m setup and a 15s order-flow trigger together.",
    generate=_pipeline("mtf_15s_trigger"),
    timeframe="1m",
    blocked="needs trade-level (aggTrade) history; the kline cache has no ticks, "
            "and 15s bars are not faked from 1m candles",
))


register(Hypothesis(
    id="cvd_flip",
    description="Liquidity sweep and compression breakout with the CVD confirmation "
                "reversed, on 15m bars, nothing else enabled.",
    generate=_pipeline("cvd_flip"),
    timeframe="15m",
))


register(Hypothesis(
    id="maker_cvd_reversion",
    description="The cvd_flip setups entered with simulated post-only orders "
                "(60% fill when price trades through, one-candle wait), maker costs.",
    generate=_pipeline("maker_cvd_reversion"),
    cost="maker_filled",
    timeframe="15m",
))


# -- Kronos ------------------------------------------------------------------ #
# One configuration, fixed before any result: Kronos-small, 256 bars in,
# 4 bars out, 3 samples at T=1.0 / top_p=0.9, one decision every 16 bars
# (non-overlapping 4-bar holds, spaced so 24 months stays feasible on CPU),
# 5 bps margin on top of the taker round trip.
KRONOS_STRIDE = 16


def kronos_samples(data: dict[str, list[Candle]], forecaster_for=None) -> list[Sample]:
    """Trade the Kronos signal directly and hold for the forecast horizon."""
    from maya.config import get_settings
    from maya.research.costs import TAKER
    from maya.signals.kronos_signal import ForecastCache, KronosForecaster

    settings = get_settings()
    cache = ForecastCache(settings.kronos_cache_path) if forecaster_for is None else None
    out: list[Sample] = []
    for symbol, candles in data.items():
        if len(candles) < 2:
            continue
        bar_ms = candles[1].timestamp - candles[0].timestamp
        fc = forecaster_for(symbol, bar_ms) if forecaster_for else KronosForecaster(
            symbol, bar_ms, TAKER.round_trip_bps / 10_000, repo_path=settings.kronos_repo_path,
            lookback=256, pred_len=4, samples=3, temperature=1.0, top_p=0.9, margin_bps=5.0,
            cache=cache, device=settings.kronos_device or None)
        for i in range(fc.lookback - 1, len(candles) - fc.pred_len, KRONOS_STRIDE):
            sig = fc.signal(candles[: i + 1])
            if sig is None or sig.direction is None:
                continue
            fwd = forward_return(candles, i, fc.pred_len)
            if fwd is not None:
                side = 1 if sig.direction == "long" else -1
                out.append(Sample(candles[i].timestamp, side * fwd, symbol, side,
                                  fc.pred_len * bar_ms))
    return out


def _kronos_precheck() -> str:
    from maya.config import get_settings
    from maya.signals.kronos_signal import unavailable_reason
    return unavailable_reason(get_settings().kronos_repo_path)


register(Hypothesis(
    id="kronos_small_15m",
    description="Kronos-small forecast 4 bars ahead on 15m; trade when the forecast "
                "clears taker round trip plus 5 bps; hold 4 bars.",
    generate=kronos_samples,
    timeframe="15m",
    precheck=_kronos_precheck,
))


# -- Chart drawings ------------------------------------------------------------ #
# One configuration, fixed 2026-11-17 before any result: at each closed 15m bar,
# read the last 200 bars with chart_patterns.read_chart; when that bar's close
# breaks a trendline or confirms a pattern's neckline (zones excluded: Atlas
# was already measured), trade the breakout's direction and hold 4 bars. Skip
# the bar if breakouts disagree. No new entry while a hold is running.
CHART_WINDOW = 200
CHART_HOLD = 4


def chart_breakout_samples(data: dict[str, list[Candle]]) -> list[Sample]:
    from maya.signals.chart_patterns import read_chart

    out: list[Sample] = []
    for symbol, candles in data.items():
        i = CHART_WINDOW - 1
        while i < len(candles) - CHART_HOLD:
            reading = read_chart(candles[i - CHART_WINDOW + 1: i + 1])
            dirs = {b.direction for b in reading.breakouts if b.kind != "zone"}
            if len(dirs) == 1:
                fwd = forward_return(candles, i, CHART_HOLD)
                if fwd is not None:
                    side = 1 if dirs == {"up"} else -1
                    out.append(Sample(candles[i].timestamp, side * fwd, symbol, side,
                                      candles[i + CHART_HOLD].timestamp - candles[i].timestamp))
                    i += CHART_HOLD
                    continue
            i += 1
    return out


register(Hypothesis(
    id="chart_breakout_15m",
    description="Close beyond a mechanical trendline or a confirmed pattern neckline "
                "(double top/bottom, head and shoulders, flag) on 15m; trade the break, "
                "hold 4 bars, taker costs.",
    generate=chart_breakout_samples,
    timeframe="15m",
))


# -- Trend following ----------------------------------------------------------- #
# One configuration, fixed 2026-11-18 before any result: on daily bars, at
# each decision close, go long if the coin closed higher than 30 days earlier
# and short if lower; hold 7 days; decide again only when the hold ends (one
# position per coin, no overlap). Funding is charged against every hold at
# 1 bps per 8h (21 periods a week, about 21 bps), on top of taker costs. The
# 15 coins move together, so the samples are not independent across coins and
# the t-stat reads high; the eight-period sign rule and the holdout are what
# check that.
TREND_LOOKBACK = 30
TREND_HOLD = 7
TREND_FUNDING_BPS_PER_8H = 1.0


def trend_samples(data: dict[str, list[Candle]]) -> list[Sample]:
    funding = TREND_HOLD * 3 * TREND_FUNDING_BPS_PER_8H / 10_000
    out: list[Sample] = []
    for symbol, candles in data.items():
        i = TREND_LOOKBACK
        while i < len(candles) - TREND_HOLD:
            past = candles[i - TREND_LOOKBACK].close
            now = candles[i].close
            fwd = forward_return(candles, i, TREND_HOLD)
            if fwd is None or past <= 0 or now == past:
                i += 1
                continue
            side = 1 if now > past else -1
            out.append(Sample(candles[i].timestamp, side * fwd - funding, symbol, side,
                              candles[i + TREND_HOLD].timestamp - candles[i].timestamp))
            i += TREND_HOLD
    return out


register(Hypothesis(
    id="trend_30d_hold_7d",
    description="Daily bars: long if the close is above the close 30 days ago, short if "
                "below; hold 7 days, no overlap; taker costs plus funding; 15 scanner coins.",
    generate=trend_samples,
    timeframe="1d",
    symbols=SCANNER_15,
))


# Declared 2026-11-19 after trend_30d_hold_7d failed on 2024-08..2026-08
# (+51.9 bps net, t +1.13, 5 of 8 periods). Same function, same rules, same
# coins, nothing changed; only the data is earlier and was never looked at:
# 2020-01-01 to 2024-08-09, ending the day before the first window starts.
# Coins not yet listed contribute nothing (HYPE, PUMP) or join late. Funding
# is still the fixed 1 bps per 8h, which undercharges longs in 2021, when
# rates often ran far higher; a pass here would need checking against real
# funding before it meant anything.
register(Hypothesis(
    id="trend_30d_hold_7d_2020_2024",
    description="trend_30d_hold_7d unchanged, on 2020-01 to 2024-08 data it never saw.",
    generate=trend_samples,
    timeframe="1d",
    symbols=SCANNER_15,
    start="2020-01-01",
    end="2024-08-09",
))


# -- Funding extremes ------------------------------------------------------------ #
# One configuration, fixed 2026-11-19 before any funding history was read for
# this purpose (the only funding looked at so far was the BTC/ETH/SOL files
# the backtester's funding factor used, and the trend test's cost check).
# The idea: an extreme funding rate means one side is crowded and paying to
# stay in, and crowded positions tend to unwind.
#
# On 8h bars, which close when funding settles (00/08/16 UTC):
#   - read the rate that settled at the bar's close;
#   - short if it is >= 3 bps AND at or above the 95th percentile of that
#     coin's previous 270 settlements (90 days);
#   - long if it is <= -1 bp AND at or below their 5th percentile;
#   - enter at the bar's close, hold 9 bars (3 days), no new entry on that
#     coin until the hold ends;
#   - every funding payment settled during the hold is booked at the real
#     rate (a short receives positive funding), plus taker costs.
# Window 2020-01-01 to 2026-11-15; its last 120 days (2026-07-18 onward) have
# not been read by any test and are the holdout. Coins are the 15 scanner
# coins, which carry the same survivor bias as before.
FUNDING_WINDOW = ("2020-01-01", "2026-11-15")
FUNDING_HISTORY = 270
FUNDING_HOLD = 9
FUNDING_SHORT_MIN = 0.0003
FUNDING_LONG_MAX = -0.0001
FUNDING_PCT = 0.05
_FUNDING_SLACK_MS = 60_000   # Binance stamps a settlement a few ms after the hour


def _binance_funding(symbol: str, window: tuple[str, str] = FUNDING_WINDOW
                     ) -> list[tuple[int, float]]:
    from maya.backtest.funding import fetch_funding_rates
    from maya.config import get_settings
    from maya.research.__main__ import _ms

    start, end = window
    rows = fetch_funding_rates(get_settings().model_copy(update={"symbol": symbol}),
                               _ms(start), _ms(end),
                               cache_path=f"data/funding_{symbol}_{start}_{end}.csv")
    return rows


def funding_extreme_samples(data: dict[str, list[Candle]], funding_for=None,
                            sides: tuple[int, ...] = (-1, 1)) -> list[Sample]:
    import bisect

    funding_for = funding_for or _binance_funding
    out: list[Sample] = []
    for symbol, candles in data.items():
        if len(candles) <= FUNDING_HOLD:
            continue
        rows = sorted(funding_for(symbol))
        if not rows:
            # fetch_funding_rates returns [] on a network error; trading on
            # "no funding" would quietly test nothing.
            raise RuntimeError(f"no funding history for {symbol}")
        times = [t for t, _ in rows]
        rates = [r for _, r in rows]
        bar_ms = candles[1].timestamp - candles[0].timestamp
        i = 0
        while i < len(candles) - FUNDING_HOLD:
            close_ts = candles[i].timestamp + bar_ms
            k = bisect.bisect_right(times, close_ts + _FUNDING_SLACK_MS) - 1
            # The settlement must be this bar's own (not an older one) and
            # have a full 90 days of history behind it.
            if k < FUNDING_HISTORY or close_ts - times[k] > _FUNDING_SLACK_MS:
                i += 1
                continue
            rate = rates[k]
            past = sorted(rates[k - FUNDING_HISTORY:k])
            hi = past[int((1 - FUNDING_PCT) * (FUNDING_HISTORY - 1))]
            lo = past[int(FUNDING_PCT * (FUNDING_HISTORY - 1))]
            side = -1 if (rate >= FUNDING_SHORT_MIN and rate >= hi) else \
                1 if (rate <= FUNDING_LONG_MAX and rate <= lo) else 0
            if side not in sides or candles[i].close <= 0:
                i += 1
                continue
            exit_i = i + FUNDING_HOLD
            end_ts = candles[exit_i].timestamp + bar_ms
            # Payments after entry and before exit. The one settling at the exit
            # moment is left out: the position closes on the bar's last price,
            # just before it, and a short would otherwise collect it.
            a = bisect.bisect_right(times, close_ts + _FUNDING_SLACK_MS)
            b = bisect.bisect_right(times, end_ts - _FUNDING_SLACK_MS)
            paid = sum(rates[a:b]) * side          # a long pays positive funding
            fwd = candles[exit_i].close / candles[i].close - 1
            out.append(Sample(candles[i].timestamp, side * fwd - paid, symbol, side,
                              end_ts - close_ts))
            i = exit_i
    return out


register(Hypothesis(
    id="funding_extreme_reversal",
    description="8h bars: short when funding is >= 3 bps and in the top 5% of its last "
                "90 days, long when <= -1 bp and in the bottom 5%; hold 3 days; real "
                "funding booked; taker costs; 15 scanner coins, 2020-01 to 2026-11.",
    generate=funding_extreme_samples,
    timeframe="8h",
    symbols=SCANNER_15,
    start=FUNDING_WINDOW[0],
    end=FUNDING_WINDOW[1],
))


# Declared 2026-11-19 AFTER funding_extreme_reversal failed and its result
# was split by side (exploration): longs on deeply negative funding made
# +90.5 bps net (t +2.89, n 1126), shorts on crowded longs lost. This is that
# long half alone, rules otherwise unchanged; with no shorts, a coin is no
# longer blocked by a short hold, so the trade list differs slightly. The
# research window is the one the split was read on, so a research pass here
# is close to guaranteed by selection and means little; the holdout
# (2026-07-18 to 2026-11-15, read by nothing) is the real test.
register(Hypothesis(
    id="funding_negative_long",
    description="Long side of funding_extreme_reversal only: buy when funding is <= -1 bp "
                "and in the bottom 5% of its last 90 days; hold 3 days; real funding.",
    generate=lambda data: funding_extreme_samples(data, sides=(1,)),
    timeframe="8h",
    symbols=SCANNER_15,
    start=FUNDING_WINDOW[0],
    end=FUNDING_WINDOW[1],
))


# -- Point-in-time coins --------------------------------------------------------- #
# Declared 2026-11-19. The user pointed out the multi-coin tests looked
# long-biased. Most of them did trade both sides, but SCANNER_15 was picked by
# 2026 volume: every coin in it survived and grew, which tilts every test
# against shorts. These reruns change only the coins.
#
# Universe rule, fixed before any of these tests ran: in calendar year Y, a
# coin is tradable if it was in the top 15 USD-M USDT perps by quote volume in
# December of Y-1 (Binance archive, delisted coins included; stablecoin pairs
# and index contracts left out). Computed by
# `python -m maya.data.binance_archive universe` and pasted here. The archive
# starts in January 2020, so there is no December-2019 ranking and trading
# starts in 2021; data starts 2020-09-01 so lookbacks are warm by then. A coin
# delisted mid-year simply stops producing trades when its data ends.
#
# Holdout: the last 120 days, 2026-07-18 to 2026-11-15. No test has passed
# research and read it. The earlier trend tests' research windows ran to
# 2026-08-09 on the survivor coins, so its first three weeks were seen there.
PIT_WINDOW = ("2020-09-01", "2026-11-15")
PIT_UNIVERSE: dict[int, list[str]] = {
    2021: ['BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'LTCUSDT', 'LINKUSDT', 'BCHUSDT', 'YFIUSDT',
           'SUSHIUSDT', 'XLMUSDT', 'ADAUSDT', 'GRTUSDT', 'EOSUSDT', 'BNBUSDT', 'DOTUSDT',
           'UNIUSDT'],
    2022: ['BTCUSDT', 'ETHUSDT', 'LUNAUSDT', 'SANDUSDT', 'MATICUSDT', 'SOLUSDT', 'GALAUSDT',
           '1000SHIBUSDT', 'AVAXUSDT', 'MANAUSDT', 'NEARUSDT', 'XRPUSDT', 'BNBUSDT',
           'ADAUSDT', 'DOGEUSDT'],
    2023: ['BTCUSDT', 'ETHUSDT', 'DOGEUSDT', 'BNBUSDT', 'XRPUSDT', 'LTCUSDT', 'SOLUSDT',
           'AXSUSDT', 'MASKUSDT', 'MATICUSDT', 'APEUSDT', 'LINKUSDT', 'CHZUSDT', 'BNXUSDT',
           'FTMUSDT'],
    2024: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ORDIUSDT', 'AVAXUSDT', 'TRBUSDT', 'DOGEUSDT',
           '1000BONKUSDT', 'XRPUSDT', 'ADAUSDT', 'OPUSDT', 'TIAUSDT', 'MATICUSDT', 'BNBUSDT',
           'BIGTIMEUSDT'],
    2025: ['BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'DOGEUSDT', 'SOLUSDT', '1000PEPEUSDT', 'SUIUSDT',
           'BNBUSDT', 'ADAUSDT', 'LINKUSDT', 'HBARUSDT', 'PNUTUSDT', 'WIFUSDT', 'ENAUSDT',
           '1000SHIBUSDT'],
    2026: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ZECUSDT', 'XRPUSDT', 'PIPPINUSDT', 'DOGEUSDT',
           'BEATUSDT', 'BNBUSDT', 'SUIUSDT', 'HYPEUSDT', 'LIGHTUSDT', '1000PEPEUSDT',
           'ADAUSDT', 'ASTERUSDT'],
}
PIT_SYMBOLS = ",".join(sorted({c for cs in PIT_UNIVERSE.values() for c in cs}))


def point_in_time(samples: list[Sample]) -> list[Sample]:
    """Keep a trade only if its coin was in that year's universe at entry."""
    out = SampleList(notes=getattr(samples, "notes", []))
    out.extend(s for s in samples
               if s.symbol in PIT_UNIVERSE.get(
                   datetime.fromtimestamp(s.timestamp / 1000, timezone.utc).year, ()))
    return out


register(Hypothesis(
    id="trend_30d_hold_7d_pit",
    description="trend_30d_hold_7d unchanged, on point-in-time top-15 coins (dead coins "
                "included), 2021-01 to 2026-11.",
    generate=lambda data: point_in_time(trend_samples(data)),
    timeframe="1d",
    symbols=PIT_SYMBOLS,
    start=PIT_WINDOW[0],
    end=PIT_WINDOW[1],
))

register(Hypothesis(
    id="funding_extreme_reversal_pit",
    description="funding_extreme_reversal unchanged (both sides), on point-in-time top-15 "
                "coins (dead coins included), 2021-01 to 2026-11.",
    generate=lambda data: point_in_time(funding_extreme_samples(
        data, funding_for=lambda s: _binance_funding(s, PIT_WINDOW))),
    timeframe="8h",
    symbols=PIT_SYMBOLS,
    start=PIT_WINDOW[0],
    end=PIT_WINDOW[1],
))


register(Hypothesis(
    id="trail_1r_after_2r",
    description="The 1m setups with a stop that trails 1R behind the best price once a "
                "close is 2R in profit, against baseline_1m_setups (lock only).",
    generate=_pipeline("trail_1r_after_2r"),
    timeframe="1m",
))
