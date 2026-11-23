"""Point-in-time coins and the Binance archive reader. No network: the
archive getter is a fake serving invented zip files."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

from maya.data import binance_archive as arc
from maya.research.costs import Sample
from maya.research.hypotheses import CATALOG, PIT_UNIVERSE, PIT_WINDOW, point_in_time


def _ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def _zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(lines))
    return buf.getvalue()


def _kline(ts, close, quote_vol):
    # open_time,open,high,low,close,volume,close_time,quote_volume,count,
    # taker_buy_volume,taker_buy_quote_volume,ignore
    return f"{ts},{close},{close},{close},{close},10,{ts + 1},{quote_vol},5,4,0,0"


def test_kline_files_parse_with_or_without_a_header():
    head = "open_time,open,high,low,close,volume,close_time,quote_volume,count," \
           "taker_buy_volume,taker_buy_quote_volume,ignore"
    for lines in ([_kline(1000, 5.0, 99.0)], [head, _kline(1000, 5.0, 99.0)]):
        blob = _zip(lines)
        rows = arc.monthly_klines("X", "1d", 2020, 12, getter=lambda u, b=blob: b)
        assert rows == [[1000, 5.0, 5.0, 5.0, 5.0, 10.0, 4.0, 99.0]]


def test_missing_month_is_empty_not_an_error():
    assert arc.monthly_klines("X", "1d", 2020, 12, getter=lambda u: None) == []
    assert arc.monthly_funding("X", 2020, 12, getter=lambda u: None) == []


def test_funding_file_reads_time_and_rate():
    blob = _zip(["calc_time,funding_interval_hours,last_funding_rate", "1000,8,0.0001"])
    assert arc.monthly_funding("X", 2021, 1, getter=lambda u: blob) == [(1000, 0.0001)]


def test_top_by_volume_drops_stablecoins_indices_and_delivery_contracts():
    vols = {"BTCUSDT": 9, "USDCUSDT": 8, "BTCDOMUSDT": 7, "ETHUSDT_230331": 6,
            "ETHUSDT": 5, "ETHBUSD": 4, "LUNAUSDT": 3}
    assert arc.top_by_volume(vols, n=3) == ["BTCUSDT", "ETHUSDT", "LUNAUSDT"]


def test_universe_ranks_on_the_december_before_the_year():
    asked = []

    def getter(url):
        asked.append(url)
        if "delimiter" in url:
            return (b"<IsTruncated>false</IsTruncated>"
                    b"<Prefix>data/futures/um/monthly/klines/AUSDT/</Prefix>"
                    b"<Prefix>data/futures/um/monthly/klines/BUSDT/</Prefix>")
        vol = 1.0 if "AUSDT" in url else 2.0
        return _zip([_kline(0, 1.0, vol)])
    u = arc.universe(range(2022, 2023), getter=getter, n=1)
    assert u == {2022: ["BUSDT"]}
    assert all("-2021-12.zip" in a for a in asked if a.endswith(".zip"))


def test_symbol_listing_follows_the_next_marker():
    pages = iter([b"<IsTruncated>true</IsTruncated><NextMarker>m1</NextMarker>"
                  b"<Prefix>data/futures/um/monthly/klines/AUSDT/</Prefix>",
                  b"<IsTruncated>false</IsTruncated>"
                  b"<Prefix>data/futures/um/monthly/klines/BUSDT/</Prefix>"])
    assert arc.list_symbols(getter=lambda u: next(pages)) == ["AUSDT", "BUSDT"]


def test_zero_volume_bars_after_a_delisting_are_dropped():
    # Invented rows: [ts, o, h, l, c, v, taker_buy]
    rows = [[1, 1, 1, 1, 0.73, 5e6, 2e6], [2, 0.77, 0.77, 0.77, 0.77, 0.0, 0.0],
            [3, 0.77, 0.77, 0.77, 0.77, 0.0, 0.0]]
    assert arc.drop_dead_bars(rows) == rows[:1]


def test_point_in_time_keeps_only_that_years_members():
    s = [Sample(_ms(2022, 3, 1), 0.01, "LUNAUSDT", -1, 1),    # member in 2022
         Sample(_ms(2023, 3, 1), 0.01, "LUNAUSDT", -1, 1),    # gone by 2023
         Sample(_ms(2020, 10, 1), 0.01, "BTCUSDT", 1, 1),     # no 2020 universe
         Sample(_ms(2026, 2, 1), 0.01, "HYPEUSDT", 1, 1)]
    kept = point_in_time(s)
    assert [(x.symbol, datetime.fromtimestamp(x.timestamp / 1000, timezone.utc).year)
            for x in kept] == [("LUNAUSDT", 2022), ("HYPEUSDT", 2026)]


def test_universe_includes_coins_that_later_died_and_every_member_is_loaded():
    members = {c for cs in PIT_UNIVERSE.values() for c in cs}
    assert {"LUNAUSDT", "FTMUSDT", "MATICUSDT"} <= members
    assert min(PIT_UNIVERSE) == 2021 and all(len(v) == 15 for v in PIT_UNIVERSE.values())
    for hid in ("trend_30d_hold_7d_pit", "funding_extreme_reversal_pit"):
        h = CATALOG[hid]
        assert set(h.symbols.split(",")) == members
        assert (h.start, h.end) == PIT_WINDOW
