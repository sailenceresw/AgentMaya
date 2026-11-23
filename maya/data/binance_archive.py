"""Binance USD-M public data archive (data.binance.vision), for coins the
REST API no longer serves.

The research coin lists were first picked by 2026 volume, which keeps only
coins that survived and grew and so tilts every multi-coin test against
shorts. A point-in-time universe needs the coins that later died (LUNA,
SRM, ...), and only the archive still has them.

    python -m maya.data.binance_archive universe        # top 15 per year, printed
    python -m maya.data.binance_archive fill            # caches for the research CLI

Network only from the CLI and the default ``get``; tests pass a fake.
"""
from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILES = "https://data.binance.vision"
UM = "data/futures/um/monthly"

# Not coins: stablecoin pairs and Binance index contracts.
EXCLUDE = re.compile(r"^(USDC|BUSD|TUSD|USDP|FDUSD|DAI|USDE|EUR|BTCDOM|DEFI|FOOTBALL|BLUEBIRD)USDT$")

Getter = Callable[[str], bytes | None]


def get(url: str) -> bytes | None:
    """GET ``url``; None on 404 (the archive's answer for 'no such month')."""
    # Some newer symbols are not ASCII (Chinese meme coins); quote the path.
    url = quote(url, safe=":/?&=%")
    try:
        with urlopen(Request(url, headers={"User-Agent": "maya-research"}), timeout=60) as r:
            return r.read()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def list_symbols(getter: Getter = get) -> list[str]:
    """Every symbol with monthly klines in the archive, delisted ones included."""
    out: list[str] = []
    marker = ""
    while True:
        xml = getter(f"{BUCKET}?delimiter=/&prefix={UM}/klines/&marker={marker}")
        text = (xml or b"").decode()
        out += re.findall(rf"<Prefix>{UM}/klines/([^/<]+)/</Prefix>", text)
        nxt = re.search(r"<NextMarker>([^<]+)</NextMarker>", text)
        if "<IsTruncated>true</IsTruncated>" not in text or not nxt:
            return sorted(set(out))
        marker = nxt.group(1)


def _csv_rows(blob: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    # Newer files carry a header row, older ones don't.
    return [r for r in rows if r and r[0].lstrip("-").isdigit()]


def monthly_klines(symbol: str, interval: str, year: int, month: int,
                   getter: Getter = get) -> list[list]:
    """``[ts, o, h, l, c, v, taker_buy_volume, quote_volume]`` rows, or []."""
    blob = getter(f"{FILES}/{UM}/klines/{symbol}/{interval}/"
                  f"{symbol}-{interval}-{year}-{month:02d}.zip")
    if not blob:
        return []
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
             float(r[5]), float(r[9]), float(r[7])] for r in _csv_rows(blob)]


def monthly_funding(symbol: str, year: int, month: int,
                    getter: Getter = get) -> list[tuple[int, float]]:
    """``[(funding_time_ms, rate)]`` for one month, or []. Columns are
    calc_time, funding_interval_hours, last_funding_rate."""
    blob = getter(f"{FILES}/{UM}/fundingRate/{symbol}/{symbol}-fundingRate-{year}-{month:02d}.zip")
    if not blob:
        return []
    return [(int(r[0]), float(r[2])) for r in _csv_rows(blob)]


def december_volume(symbols: list[str], year: int, getter: Getter = get,
                    workers: int = 16) -> dict[str, float]:
    """Quote volume over December of ``year`` per symbol that traded then."""
    def one(sym: str) -> tuple[str, float]:
        rows = monthly_klines(sym, "1d", year, 12, getter)
        return sym, sum(r[7] for r in rows)
    with ThreadPoolExecutor(workers) as pool:
        vols = dict(pool.map(one, symbols))
    return {s: v for s, v in vols.items() if v > 0}


def top_by_volume(vols: dict[str, float], n: int = 15) -> list[str]:
    """The ``n`` biggest USDT perps by volume, stablecoins and indices left out."""
    ok = [s for s in vols if s.endswith("USDT") and "_" not in s and not EXCLUDE.match(s)]
    return sorted(ok, key=lambda s: -vols[s])[:n]


def universe(years: range, getter: Getter = get, n: int = 15) -> dict[int, list[str]]:
    """For each year Y, the top ``n`` by volume in December of Y-1: known on
    1 January of Y, so trading year Y on it reads nothing from the future."""
    symbols = list_symbols(getter)
    return {y: top_by_volume(december_volume(symbols, y - 1, getter), n) for y in years}


def _months(start: str, end: str):
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def archive_klines(symbol: str, interval: str, start: str, end: str,
                   getter: Getter = get) -> list[list]:
    """Rows in the research cache's 7-column layout, ``start`` <= ts <= ``end``."""
    lo = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    hi = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = [r[:7] for y, m in _months(start, end)
            for r in monthly_klines(symbol, interval, y, m, getter)]
    return sorted(r for r in rows if lo <= r[0] <= hi)


def archive_funding(symbol: str, start: str, end: str,
                    getter: Getter = get) -> list[tuple[int, float]]:
    lo = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    hi = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = [r for y, m in _months(start, end) for r in monthly_funding(symbol, y, m, getter)]
    return sorted(r for r in rows if lo <= r[0] <= hi)


def drop_dead_bars(rows: list[list]) -> list[list]:
    """Remove bars with zero volume. After a delisting the REST API keeps
    serving the last price with no volume (FTMUSDT sat at 0.7702 for 678
    days, BNXUSDT at 2.0): no trading happened, so they are not data."""
    return [r for r in rows if r[5] and float(r[5]) > 0]


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m maya.data.binance_archive")
    p.add_argument("command", choices=["universe", "fill"])
    a = p.parse_args(argv)
    if a.command == "universe":
        for y, coins in universe(range(2020, 2027)).items():
            print(f"    {y}: {coins!r},")
        return 0
    from maya.research.hypotheses import PIT_UNIVERSE, PIT_WINDOW
    # fill: write the research caches for every coin that is ever a member.
    # Coins still listed come from the REST API (it reaches today; the
    # archive's monthly files stop at the last full month). Coins the API
    # no longer serves come from the archive.
    from pathlib import Path

    from maya.backtest.data_loader import _save_csv, fetch_historical_klines, klines_cache_path
    from maya.backtest.funding import fetch_funding_rates
    from maya.config import get_settings
    from maya.research.__main__ import _ms
    start, end = PIT_WINDOW
    base = get_settings()
    coins = sorted({c for cs in PIT_UNIVERSE.values() for c in cs})
    for sym in coins:
        source = "api"
        for tf in ("1d", "8h"):
            path = klines_cache_path(sym, tf, start, end)
            st = base.model_copy(update={"symbol": sym, "timeframe": tf})
            try:
                rows = fetch_historical_klines(st, _ms(start), _ms(end), cache_path=path)
            except Exception:  # noqa: BLE001 - unknown symbol on the live API
                rows = []
            if not rows:
                source = "archive"
                rows = archive_klines(sym, tf, start, end)
            live = drop_dead_bars(rows)
            if live and len(live) != len(rows) or (live and not Path(path).exists()):
                print(f"{sym} {tf}: {len(rows) - len(live)} zero-volume bars dropped",
                      file=sys.stderr)
                _save_csv(live, path)
        fpath = f"data/funding_{sym}_{start}_{end}.csv"
        if not Path(fpath).exists():
            fr = [] if source == "archive" else fetch_funding_rates(
                base.model_copy(update={"symbol": sym}), _ms(start), _ms(end), cache_path=fpath)
            if not fr:
                fr = archive_funding(sym, start, end)
                with open(fpath, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["funding_time", "funding_rate"])
                    w.writerows(fr)
        print(f"{sym}: {source}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
