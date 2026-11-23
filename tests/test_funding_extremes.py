"""funding_extreme_reversal: the rules as declared, no look-ahead, real funding.

All series here are invented; the funding fetcher is replaced, so nothing
touches the network.
"""
from __future__ import annotations

import pytest

from maya.data.models import Candle
from maya.research.hypotheses import (
    CATALOG,
    FUNDING_HISTORY,
    FUNDING_HOLD,
    funding_extreme_samples,
)

H8 = 8 * 3_600_000
N = FUNDING_HISTORY + 40


def _bars(n=N, price=lambda i: 100.0):
    return [Candle(timestamp=i * H8, open=price(i), high=price(i), low=price(i),
                   close=price(i), volume=1) for i in range(n)]


def _funding(spikes: dict[int, float], base=0.0001, n=N + 1):
    """One settlement at each bar's close (stamped 3 ms late, as Binance does)."""
    return [((i + 1) * H8 + 3, spikes.get(i, base)) for i in range(n)]


def _run(bars, rows):
    return funding_extreme_samples({"X": bars}, funding_for=lambda s: rows)


def test_quiet_funding_trades_nothing():
    assert _run(_bars(), _funding({})) == []


def test_crowded_longs_are_shorted_and_short_collects_funding():
    i = FUNDING_HISTORY + 5
    falls = _bars(price=lambda j: 100.0 if j <= i else 90.0)
    s = _run(falls, _funding({i: 0.001}))
    assert len(s) == 1 and s[0].direction == -1 and s[0].timestamp == i * H8
    # Price -10% (a short gains 10%) plus 8 payments of 1 bp collected; the
    # payment at the exit moment is not.
    assert s[0].gross_return == pytest.approx(0.10 + 8 * 0.0001)
    assert s[0].hold_ms == FUNDING_HOLD * H8 and s[0].symbol == "X"


def test_deeply_negative_funding_is_bought_and_the_long_pays_nothing_back():
    i = FUNDING_HISTORY + 3
    rows = _funding({i: -0.002} | {j: -0.0005 for j in range(i + 1, i + 9)})
    s = _run(_bars(), rows)
    assert len(s) == 1 and s[0].direction == 1
    # Flat price; the long receives 8 negative payments of 5 bps.
    assert s[0].gross_return == pytest.approx(8 * 0.0005)


def test_a_high_rate_that_is_normal_for_the_coin_is_not_extreme():
    # 2.5 bps is above every past rate (top 5% for this coin) but under the
    # 3 bps floor; the rule needs both, so no trade.
    i = FUNDING_HISTORY + 5
    rows = _funding({i: 0.00025}, base=0.0002)       # above every past rate, but < 3 bps
    assert _run(_bars(), rows) == []


def test_needs_90_days_of_history_first():
    rows = _funding({FUNDING_HISTORY - 10: 0.01})
    assert _run(_bars(), rows) == []


def test_one_trade_at_a_time_per_coin():
    i = FUNDING_HISTORY + 2
    rows = _funding({i: 0.001, i + 3: 0.001, i + FUNDING_HOLD + 1: 0.001})
    s = _run(_bars(), rows)
    assert [x.timestamp // H8 for x in s] == [i, i + FUNDING_HOLD + 1]


def test_signal_does_not_read_later_funding_or_prices():
    i = FUNDING_HISTORY + 5
    rows = _funding({i: 0.001})
    a = _run(_bars(), rows)
    # Change everything after the trade's exit: the trade must not change.
    later = i + FUNDING_HOLD + 1
    rows2 = [(t, r if t <= later * H8 else 0.05) for t, r in rows]
    bars2 = _bars(price=lambda j: 100.0 if j < later else 500.0)
    b = _run(bars2, rows2)
    assert b[0] == a[0]


def test_a_stale_settlement_is_not_reused():
    # A gap in the funding file: the bar must not trade on an old rate.
    i = FUNDING_HISTORY + 5
    rows = [r for r in _funding({i - 1: 0.001}) if r[0] != (i + 1) * H8 + 3]
    s = _run(_bars(), rows)
    assert all(x.timestamp != i * H8 for x in s)


def test_missing_funding_history_is_an_error_not_an_empty_test():
    with pytest.raises(RuntimeError):
        _run(_bars(), [])


def test_long_only_variant_skips_shorts_and_is_not_blocked_by_them():
    i = FUNDING_HISTORY + 2
    rows = _funding({i: 0.001, i + 3: -0.003})       # crowded longs, then crowded shorts
    both = _run(_bars(), rows)
    assert [x.direction for x in both] == [-1]       # the short blocks the later long
    longs = funding_extreme_samples({"X": _bars()}, funding_for=lambda s: rows, sides=(1,))
    assert [(x.direction, x.timestamp // H8) for x in longs] == [(1, i + 3)]
    h = CATALOG["funding_negative_long"]
    assert (h.timeframe, h.start, h.end, h.symbols) == (
        "8h", "2020-01-01", "2026-11-15", CATALOG["funding_extreme_reversal"].symbols)


def test_declaration():
    h = CATALOG["funding_extreme_reversal"]
    assert (h.timeframe, h.start, h.end) == ("8h", "2020-01-01", "2026-11-15")
    assert len(h.symbols.split(",")) == 15 and h.cost == "taker"
