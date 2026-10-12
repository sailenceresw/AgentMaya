"""Multi-coin scanner: universe, fee gate, desk veto, ranking — all offline.

The setup-bearing coin is the demo ScenarioFeed cut at the bar where the
strategy fires, so the scanner finds a *genuine* setup through the same
SignalProcessor/StrategyEngine the agent trades with; nothing about the
strategy is mocked.
"""
from __future__ import annotations

import pytest

from maya.config import RunMode, Settings, Venue
from maya.data.scenarios import ScenarioFeed
from maya.scanner import Market, Scanner, format_table, parse_hyperliquid_markets
from maya.signals.processor import SignalProcessor
from maya.strategy.engine import StrategyEngine


def _settings(**kw) -> Settings:
    base = dict(mode=RunMode.PAPER, venue=Venue.HYPERLIQUID, start_equity=1000,
                leverage=20, min_roi_pct=10.0, scan_min_volume_usd=1e6, scan_max_coins=5)
    base.update(kw)
    return Settings(**base)


@pytest.fixture(autouse=True)
def short_history(monkeypatch):
    # The scripted setup fires on bar 11, well under the live 50-bar floor.
    monkeypatch.setattr("maya.scanner.MIN_HISTORY", 5)


@pytest.fixture(scope="module")
def setup_candles():
    """Scenario candles ending on the bar where a real setup fires."""
    s = Settings(mode=RunMode.SIM, scenario=True)
    candles = list(ScenarioFeed(s).candles())
    proc, strat = SignalProcessor(s.symbol, htf_timeframes=s.htf_list), StrategyEngine(s)
    for i, c in enumerate(candles):
        if strat.evaluate(proc.process(c)) is not None:
            return candles[: i + 1]
    pytest.fail("the scenario no longer produces a setup")


def _markets():
    return [
        Market("BTC", 2e9, 40, -0.00001, 66800.0),
        Market("ETH", 9e8, 25, 0.00001, 3150.0),
        Market("THIN", 5e5, 10, 0.0, 1.0),        # below the volume floor
        Market("LOWLEV", 5e8, 10, 0.0, 2.0),      # cannot take 20x
    ]


def _scanner(setup_candles, **kw):
    flat = [c for c in setup_candles[:-1]]       # the same history minus the trigger bar

    def candles(symbol, count):
        return list(setup_candles if symbol == "BTCUSDT" else flat)[-count:]

    return Scanner(_settings(**kw), fetch_markets=_markets, fetch_candles=candles)


def test_universe_is_liquid_leverage_capable_and_busiest_first(setup_candles):
    coins = [m.coin for m in _scanner(setup_candles).universe()]
    assert coins == ["BTC", "ETH"]


def test_scan_finds_the_real_setup_and_ranks_it_first(setup_candles):
    results = _scanner(setup_candles).scan(now_ts=setup_candles[-1].timestamp)
    best = results[0]
    assert best.market.coin == "BTC" and best.tradable
    assert best.net_rr > 3.0 and best.fee_share < 0.2
    assert results[1].rejected == "no setup on the last closed candle"


def test_fee_gate_can_reject_a_setup_the_strategy_liked(setup_candles):
    # Demand an impossible net R:R: the setup is found, then refused on fees.
    best = _scanner(setup_candles, min_net_rr=50.0).scan(
        now_ts=setup_candles[-1].timestamp)[0]
    assert best.intent is not None and not best.tradable
    assert "net R:R" in best.rejected


def test_desk_veto_removes_a_setup_the_chart_leans_against(setup_candles, monkeypatch):
    from maya.agents import desk
    from maya.agents.models import Conference

    scanner = _scanner(setup_candles)
    real = desk.confer

    def against(candles, sig, *a, **k):
        conf = real(candles, sig, *a, **k)
        # The scenario trade is a short; a bullish desk opposes it.
        return Conference("bullish", 0.9, conf.findings, rationale="Atlas bullish")

    monkeypatch.setattr(desk, "confer", against)
    best = scanner.scan(now_ts=setup_candles[-1].timestamp)[0]
    assert not best.tradable and "desk disagrees" in best.rejected

    scanner.settings = scanner.settings.model_copy(update={"scan_desk_veto": False})
    assert scanner.scan(now_ts=setup_candles[-1].timestamp)[0].tradable


def test_one_broken_coin_does_not_stop_the_scan(setup_candles):
    def candles(symbol, count):
        if symbol == "ETHUSDT":
            raise RuntimeError("symbol not found")
        return setup_candles[-count:]

    scanner = Scanner(_settings(), fetch_markets=_markets, fetch_candles=candles)
    results = scanner.scan(now_ts=setup_candles[-1].timestamp)
    assert results[0].tradable
    assert any("no candles" in r.rejected for r in results)


def test_pinned_symbols_bypass_the_market_list(setup_candles):
    def boom():
        raise AssertionError("market list must not be fetched")

    scanner = Scanner(_settings(scan_symbols="BTCUSDT, sol"), fetch_markets=boom,
                      fetch_candles=lambda s, n: [])
    assert [m.coin for m in scanner.universe()] == ["BTC", "SOL"]


def test_parse_hyperliquid_markets_reads_the_public_payload():
    meta = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                         {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
                         {"name": "OLD", "szDecimals": 0, "maxLeverage": 3,
                          "isDelisted": True}]}
    ctxs = [{"dayNtlVlm": "1500000000.5", "funding": "0.0000125", "markPx": "66800.0"},
            {"dayNtlVlm": "90000000", "funding": "-0.00002", "markPx": "0.0123"},
            {"dayNtlVlm": "1", "funding": "0", "markPx": "1"}]
    markets = parse_hyperliquid_markets(meta, ctxs)
    assert [m.coin for m in markets] == ["BTC", "kPEPE"]
    assert markets[0].volume_24h_usd == pytest.approx(1.5e9)
    assert markets[1].binance_symbol == "1000PEPEUSDT"


def test_table_renders(setup_candles):
    out = format_table(_scanner(setup_candles).scan(now_ts=setup_candles[-1].timestamp))
    assert "TRADE" in out and "BTC" in out


# -- the scan-or-manage loop -------------------------------------------------- #
def test_scan_agent_opens_the_best_coin_then_manages_it_to_a_close(setup_candles):
    from maya.data.models import Candle
    from maya.execution.engine import ExecutionEngine
    from maya.journal.store import TradeJournal
    from maya.risk.manager import RiskManager
    from maya.scan_run import ScanAgent

    settings = _settings()
    risk = RiskManager(settings, 1000)
    scanner = _scanner(setup_candles)
    scanner.risk = risk
    execution = ExecutionEngine(settings)          # PaperBroker in paper mode
    journal = TradeJournal(db_path=":memory:")
    agent = ScanAgent(settings, journal, scanner, execution, risk)

    opened = agent.step(now_ts=setup_candles[-1].timestamp)
    assert opened is not None and execution.position.symbol == "BTCUSDT"

    # While a position is open the agent manages it instead of scanning.
    pos = execution.position
    target_bar = Candle(timestamp=setup_candles[-1].timestamp + 60_000,
                        open=pos.entry_price, high=pos.entry_price,
                        low=pos.take_profit * 0.999, close=pos.take_profit,
                        volume=10.0)
    scanner.fetch_candles = lambda symbol, n: [target_bar]
    assert agent.step(now_ts=target_bar.timestamp) is None
    assert execution.position is None

    m = journal.metrics()
    assert m["trades"] == 1 and m["win_rate"] == 100.0
    assert m["total_fees"] > 0     # the round trip was charged


def test_scan_agent_stands_aside_when_nothing_pays(setup_candles):
    from maya.execution.engine import ExecutionEngine
    from maya.journal.store import TradeJournal
    from maya.risk.manager import RiskManager
    from maya.scan_run import ScanAgent

    settings = _settings(min_net_rr=50.0)
    risk = RiskManager(settings, 1000)
    scanner = _scanner(setup_candles, min_net_rr=50.0)
    scanner.risk = risk
    execution = ExecutionEngine(settings)
    agent = ScanAgent(settings, TradeJournal(db_path=":memory:"), scanner, execution, risk)
    assert agent.step(now_ts=setup_candles[-1].timestamp) is None
    assert not execution.has_position


def test_dashboard_snapshot_includes_the_latest_scan_and_fees(setup_candles):
    from maya.api.server import build_snapshot
    from maya.execution.engine import ExecutionEngine
    from maya.journal.store import TradeJournal
    from maya.risk.manager import RiskManager
    from maya.scan_run import ScanAgent

    settings = _settings()
    risk = RiskManager(settings, 1000)
    scanner = _scanner(setup_candles)
    scanner.risk = risk
    journal = TradeJournal(db_path=":memory:")
    agent = ScanAgent(settings, journal, scanner, ExecutionEngine(settings), risk)
    agent.step(now_ts=setup_candles[-1].timestamp)

    snap = build_snapshot(settings, agent, journal, agent.steps)
    assert snap["scan"][0]["coin"] == "BTC" and snap["scan"][0]["status"] == "trade"
    assert snap["config"]["round_trip_fee_pct"] == pytest.approx(0.26)
    assert snap["config"]["agent"] == "scanner"
    assert snap["position"]["symbol"] == "BTCUSDT"
