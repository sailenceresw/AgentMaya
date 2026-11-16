"""Three-timeframe reading: 15m bias, 1m staging, 15s trigger.

Each layer is a small pure function with its rule fixed here, so the rule
that gets tested is the rule that trades:

- **Bias (15m).** ``long`` when the last closed 15m bar is above the session
  VWAP and the confirmed swing structure is making higher highs. ``short`` on
  the mirror. Anything else is ``none``. Two inputs, no thresholds to tune.
- **Staging (1m).** The existing setups, unchanged. With the bias filter on,
  a setup may only fire in the direction of the bias.
- **Trigger (15s).** 15-second bars are built from the trade stream, because
  exchange kline streams stop at one minute. The trigger fires when the last
  15s bar's delta points the trade's way and price has held the setup's stop
  level through that bar.

The trigger is evaluated when the 1m candle closes, on the ticks of that
minute. It confirms the staged entry with the final 15 seconds of order flow;
it does not enter mid-minute, because the graph runs once per closed candle.

Neither layer has been measured. Both are off by default and each has a
hypothesis in ``maya.research``. Backtest klines carry no ticks, so the 15s
trigger cannot be backtested until trade-level history is added.
"""
from __future__ import annotations

from dataclasses import dataclass

from maya.data.models import Candle, Trade
from maya.signals.models import HTFContext

BIAS_TIMEFRAME = "15m"
TRIGGER_BAR_MS = 15_000
_DAY_MS = 86_400_000


def session_vwap(candles: list[Candle]) -> float | None:
    """Volume-weighted average price of the UTC day the last candle is in.

    Uses the typical price (high + low + close) / 3 per bar. ``candles`` must
    be closed bars. None when the session has no volume yet.
    """
    if not candles:
        return None
    day = candles[-1].timestamp // _DAY_MS
    pv = vol = 0.0
    for c in reversed(candles):
        if c.timestamp // _DAY_MS != day:
            break
        pv += (c.high + c.low + c.close) / 3.0 * c.volume
        vol += c.volume
    return pv / vol if vol > 0 else None


def bias(ctx: HTFContext | None) -> str:
    """``long``, ``short`` or ``none`` from one 15m context."""
    if ctx is None or ctx.session_vwap is None or not ctx.price:
        return "none"
    if ctx.price > ctx.session_vwap and ctx.structure == "higher_highs":
        return "long"
    if ctx.price < ctx.session_vwap and ctx.structure == "lower_lows":
        return "short"
    return "none"


@dataclass
class TriggerBar:
    """One 15-second bar with its order flow."""

    timestamp: int          # ms, bar open
    open: float
    high: float
    low: float
    close: float
    buy_volume: float = 0.0     # aggressive buys
    sell_volume: float = 0.0    # aggressive sells

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def volume(self) -> float:
        return self.buy_volume + self.sell_volume


def build_trigger_bars(trades: list[Trade], bar_ms: int = TRIGGER_BAR_MS) -> list[TriggerBar]:
    """Fold trade ticks into fixed-width bars aligned to the epoch.

    Side comes from the exchange's flag: ``is_buyer_maker`` true means the
    seller was the aggressor. Windows with no trades produce no bar; an empty
    bar would carry a delta of zero that looks like balanced flow when it is
    really no flow.
    """
    bars: dict[int, TriggerBar] = {}
    for t in sorted(trades, key=lambda x: x.timestamp):
        ts = (t.timestamp // bar_ms) * bar_ms
        bar = bars.get(ts)
        if bar is None:
            bar = bars[ts] = TriggerBar(ts, t.price, t.price, t.price, t.price)
        bar.high = max(bar.high, t.price)
        bar.low = min(bar.low, t.price)
        bar.close = t.price
        if t.is_buyer_maker:
            bar.sell_volume += t.qty
        else:
            bar.buy_volume += t.qty
    return [bars[k] for k in sorted(bars)]


def trigger_fires(direction: str, stop_level: float, bars: list[TriggerBar]) -> bool:
    """Does the last 15s bar confirm an entry in ``direction``?

    Long: its delta is positive and its low stayed above the stop level.
    Short: its delta is negative and its high stayed below the stop level.
    No bars means no confirmation.
    """
    if not bars:
        return False
    last = bars[-1]
    if direction == "long":
        return last.delta > 0 and last.low > stop_level
    return last.delta < 0 and last.high < stop_level
