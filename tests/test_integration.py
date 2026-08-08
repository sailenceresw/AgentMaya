"""End-to-end test: the scripted scenario must drive one winning trade through
the full LangGraph pipeline (signals -> strategy -> risk -> execution -> journal)
using the unmodified strategy and risk logic."""
from maya.config import RunMode, Settings
from maya.data.scenarios import ScenarioFeed
from maya.graph import build_graph
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes


def test_scenario_executes_one_winning_trade(tmp_path):
    settings = Settings(mode=RunMode.SIM, scenario=True, start_equity=1000, max_candles=0)
    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    nodes = TradingNodes(settings, journal)
    graph = build_graph(nodes)

    for candle in ScenarioFeed(settings).candles():
        graph.invoke({"candle": candle})

    metrics = journal.metrics()
    assert metrics["trades"] == 1
    assert metrics["win_rate"] == 100.0
    assert metrics["total_pnl_usdt"] > 0
    # Risk layer must never have exceeded the single-position limit.
    assert nodes.risk.account.open_positions == 0
    journal.close()


def test_sim_feed_runs_without_error(tmp_path):
    settings = Settings(mode=RunMode.SIM, start_equity=1000, max_candles=120)
    from maya.data.ingestion import SimulatedFeed

    journal = TradeJournal(db_path=str(tmp_path / "j2.db"))
    nodes = TradingNodes(settings, journal)
    graph = build_graph(nodes)
    n = 0
    for candle in SimulatedFeed(settings).candles():
        graph.invoke({"candle": candle})
        n += 1
    assert n == 120
    journal.close()
