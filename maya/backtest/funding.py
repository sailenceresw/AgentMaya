"""Historical funding rates for the backtester.

The ``funding`` confluence factor asks whether funding is paying the side we
want to take — positive funding means longs are paying shorts, which supports
a short. Until now ``SignalObject.funding_rate`` was hard-wired to 0.0 and
``funding_aligns`` requires a strict ``>0``/``<0``, so the factor could never
fire in either direction and silently cost the score a point on every trade.

Perp funding settles on a schedule (8h on Binance USD-M), so the whole series
for a multi-month backtest is a few hundred rows — nothing like tick data.
"""
from __future__ import annotations

import bisect
import csv
from pathlib import Path

from loguru import logger

from maya.config import Settings

_LIMIT = 1000  # max rows per funding-rate page


def fetch_funding_rates(settings: Settings, start_ms: int, end_ms: int,
                        cache_path: str | None = None) -> list[tuple[int, float]]:
    """``[(funding_time_ms, rate), ...]`` ascending, from Binance's public
    funding history. Cached to CSV like klines so repeat runs are offline."""
    if cache_path and Path(cache_path).exists():
        with open(cache_path, newline="") as f:
            r = csv.reader(f)
            next(r, None)
            return [(int(row[0]), float(row[1])) for row in r]

    import ccxt

    ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
    symbol = settings.symbol.replace("/", "")
    out: list[tuple[int, float]] = []
    since = start_ms
    while since < end_ms:
        try:
            page = ex.fapiPublicGetFundingRate({
                "symbol": symbol, "startTime": since,
                "endTime": end_ms, "limit": _LIMIT,
            })
        except Exception as exc:  # noqa: BLE001 - funding is a bonus factor
            logger.warning("funding history unavailable ({}); the funding "
                           "confluence factor will stay inactive", exc)
            return []
        if not page:
            break
        out.extend((int(r["fundingTime"]), float(r["fundingRate"])) for r in page)
        nxt = int(page[-1]["fundingTime"]) + 1
        if nxt <= since:
            break
        since = nxt
        if len(page) < _LIMIT:
            break

    out.sort()
    logger.info("Fetched {} funding rows for {}", len(out), symbol)
    if cache_path and out:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["funding_time", "funding_rate"])
            w.writerows(out)
    return out


class FundingSchedule:
    """Looks up the funding rate in force at a given candle timestamp.

    Uses the most recent *settled* rate at or before the timestamp, never the
    next one — the upcoming rate is not knowable at that moment, and reading
    it would leak the future into every signal that touches funding.
    """

    def __init__(self, rows: list[tuple[int, float]] | None = None) -> None:
        rows = sorted(rows or [])
        self._times = [t for t, _ in rows]
        self._rates = [r for _, r in rows]

    def __len__(self) -> int:
        return len(self._times)

    def rate_at(self, ts_ms: int) -> float:
        """Funding rate in force at ``ts_ms``; 0.0 if nothing has settled yet."""
        if not self._times:
            return 0.0
        i = bisect.bisect_right(self._times, ts_ms) - 1
        return self._rates[i] if i >= 0 else 0.0
