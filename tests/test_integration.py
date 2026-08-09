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


def _fake_result(pnl: float, result: str = "win"):
    """Minimal TradeResult for journal tests."""
    from maya.execution.engine import TradeResult
    from maya.execution.models import Position

    pos = Position(
        symbol="BTCUSDT", direction="short", setup_name="liquidity_sweep_choch",
        entry_price=66800.0, stop_loss=67388.0, take_profit=63700.0,
        quantity=0.00599, notional=400.0, leverage=20, opened_ts=1700000600000,
        confluence_score=7, rr_planned=5.2,
    )
    return TradeResult(pos, 63700.0, pnl, 0.4, 5.2, result, 1700000900000, "take_profit")


def test_run_metrics_are_scoped_to_this_process_not_the_whole_journal(tmp_path):
    """The journal is durable across restarts by design (it backs the spec-8.4
    risk-state restoration), so reporting its all-time aggregate as the run
    summary is misleading — a run that took no trades would still print a
    previous run's PnL, and the §14.1 profit-factor/win-rate gates would be
    computed over sim, testnet and live runs mixed together.
    """
    db = str(tmp_path / "j.db")

    earlier = TradeJournal(db_path=db)          # a previous run
    earlier.record(_fake_result(18.16), [])
    earlier.close()

    current = TradeJournal(db_path=db)          # a fresh process
    assert current.metrics(session_only=True) == {"trades": 0}
    assert current.metrics()["trades"] == 1     # ...but the history is still there

    current.record(_fake_result(-5.0, "loss"), [])
    run = current.metrics(session_only=True)
    lifetime = current.metrics()
    current.close()

    assert run["trades"] == 1
    assert run["total_pnl_usdt"] == -5.0        # only what this process did
    assert lifetime["trades"] == 2
    assert lifetime["total_pnl_usdt"] == round(18.16 - 5.0, 2)


def test_session_metrics_cannot_be_defeated_by_synthetic_timestamps(tmp_path):
    """Scoping by timestamp would break here: the sim and scenario feeds stamp
    trades in 2023, so a `timestamp_exit >= process_start` filter would discard
    the very trades the run just made."""
    import time

    journal = TradeJournal(db_path=str(tmp_path / "j.db"))
    journal.record(_fake_result(18.16), [])     # stamped 2023 by the scripted feed
    assert journal.metrics(session_only=True)["trades"] == 1
    assert _fake_result(18.16).exit_ts < int(time.time() * 1000)
    journal.close()
