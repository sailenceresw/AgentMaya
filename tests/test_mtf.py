"""Multi-timeframe layers: 15m bias, bias filter, 15s bars and trigger.

All prices and ticks are invented fixtures.
"""
from __future__ import annotations

import pytest

from maya.config import RunMode, Settings
from maya.data.models import Candle, Trade
from maya.journal.store import TradeJournal
from maya.nodes import TradingNodes
from maya.research import __main__ as cli
from maya.research.hypotheses import CATALOG, pipeline_samples
from maya.signals import mtf
from maya.signals.models import HTFContext, SignalObject
from maya.signals.processor import SignalProcessor
from maya.strategy.engine import StrategyEngine
from maya.strategy.models import TradeIntent

DAY = 86_400_000
M15 = 900_000


def _c(ts, close, vol=10.0, spread=1.0):
    return Candle(timestamp=ts, open=close, high=close + spread, low=close - spread,
                  close=close, volume=vol)


# -- session VWAP ----------------------------------------------------------- #
def test_session_vwap_weights_by_volume():
    bars = [_c(0, 100, vol=1), _c(M15, 110, vol=3)]
    assert mtf.session_vwap(bars) == pytest.approx((100 * 1 + 110 * 3) / 4)


def test_session_vwap_resets_at_the_utc_day():
    bars = [_c(DAY - M15, 50, vol=100), _c(DAY, 100, vol=1), _c(DAY + M15, 102, vol=1)]
    assert mtf.session_vwap(bars) == pytest.approx(101.0)   # yesterday's bar ignored


def test_session_vwap_is_none_without_volume():
    assert mtf.session_vwap([]) is None
    assert mtf.session_vwap([_c(0, 100, vol=0)]) is None


# -- bias ------------------------------------------------------------------- #
def _ctx(price, vwap, structure):
    return HTFContext(timeframe="15m", price=price, session_vwap=vwap, structure=structure)


@pytest.mark.parametrize("price,vwap,structure,expected", [
    (101, 100, "higher_highs", "long"),
    (99, 100, "lower_lows", "short"),
    (101, 100, "lower_lows", "none"),       # above VWAP but structure disagrees
    (99, 100, "higher_highs", "none"),
    (101, 100, "range", "none"),
    (100, 100, "higher_highs", "none"),     # on the VWAP is neither side
    (101, None, "higher_highs", "none"),
])
def test_bias_needs_both_inputs_to_agree(price, vwap, structure, expected):
    assert mtf.bias(_ctx(price, vwap, structure)) == expected


def test_bias_is_none_without_a_15m_context():
    assert mtf.bias(None) == "none"


def test_processor_publishes_session_vwap_only_from_closed_15m_bars():
    proc = SignalProcessor("BTCUSDT", htf_timeframes=("15m",))
    sig = None
    for i in range(15):                                   # first bucket still forming
        sig = proc.process(_c(i * 60_000, 100 + i))
    assert "15m" not in sig.htf
    sig = proc.process(_c(15 * 60_000, 200))              # next bucket opens, first closes
    assert sig.htf["15m"].session_vwap is not None
    assert sig.htf["15m"].session_vwap < 120              # the 200 bar is not in it yet


# -- bias filter in the strategy ------------------------------------------- #
def _engine(monkeypatch, direction, **kw):
    engine = StrategyEngine(Settings(mode=RunMode.SIM, llm_enabled=False, **kw))
    intent = TradeIntent("BTCUSDT", direction, "liquidity_sweep_choch", 100, 99.5, 102, 103,
                         confluence_score=0, expected_rr=4.0)
    monkeypatch.setattr(engine, "_setups", (lambda sig, ctx: intent,))
    monkeypatch.setattr("maya.strategy.engine.score_confluence",
                        lambda *a, **k: (9, ["setup_match"]))
    return engine


def _sig(bias_ctx=None):
    sig = SignalObject(timestamp=0, symbol="BTCUSDT", price=100, high=101, low=99)
    if bias_ctx is not None:
        sig.htf["15m"] = bias_ctx
    return sig


def test_filter_off_leaves_setups_alone(monkeypatch):
    engine = _engine(monkeypatch, "long")
    assert engine.evaluate(_sig(_ctx(99, 100, "lower_lows"))) is not None


def test_filter_passes_a_setup_that_agrees_with_the_bias(monkeypatch):
    engine = _engine(monkeypatch, "long", mtf_bias_filter_enabled=True)
    assert engine.evaluate(_sig(_ctx(101, 100, "higher_highs"))).direction == "long"


def test_filter_drops_a_setup_against_the_bias(monkeypatch):
    engine = _engine(monkeypatch, "long", mtf_bias_filter_enabled=True)
    assert engine.evaluate(_sig(_ctx(99, 100, "lower_lows"))) is None


def test_no_bias_means_no_trade(monkeypatch):
    engine = _engine(monkeypatch, "short", mtf_bias_filter_enabled=True)
    assert engine.evaluate(_sig(_ctx(101, 100, "range"))) is None
    assert engine.evaluate(_sig()) is None                # no 15m bar closed yet


# -- 15s bars --------------------------------------------------------------- #
def _t(ms, price, qty, sell=False):
    return Trade(timestamp=ms, price=price, qty=qty, is_buyer_maker=sell)


def test_bars_split_buy_and_sell_volume_by_the_side_flag():
    bars = mtf.build_trigger_bars([
        _t(1_000, 100, 2.0), _t(5_000, 101, 1.0, sell=True), _t(14_999, 100.5, 0.5),
        _t(15_000, 102, 4.0, sell=True),
    ])
    assert [b.timestamp for b in bars] == [0, 15_000]
    first = bars[0]
    assert (first.open, first.high, first.low, first.close) == (100, 101, 100, 100.5)
    assert (first.buy_volume, first.sell_volume) == (2.5, 1.0)
    assert first.delta == 1.5 and first.volume == 3.5
    assert bars[1].delta == -4.0


def test_bars_are_epoch_aligned_and_sorted_whatever_the_tick_order():
    bars = mtf.build_trigger_bars([_t(46_000, 3, 1), _t(31_000, 2, 1), _t(16_000, 1, 1)])
    assert [b.timestamp for b in bars] == [15_000, 30_000, 45_000]


def test_a_window_without_trades_makes_no_bar():
    bars = mtf.build_trigger_bars([_t(1_000, 100, 1), _t(46_000, 101, 1)])
    assert [b.timestamp for b in bars] == [0, 45_000]
    assert mtf.build_trigger_bars([]) == []


# -- trigger ---------------------------------------------------------------- #
def _bar(delta, low=100.0, high=101.0):
    buy, sell = (delta, 0.0) if delta > 0 else (0.0, -delta)
    return mtf.TriggerBar(0, 100.5, high, low, 100.5, buy, sell)


def test_long_trigger_needs_positive_delta_and_a_held_stop():
    assert mtf.trigger_fires("long", 99.5, [_bar(+2)])
    assert not mtf.trigger_fires("long", 99.5, [_bar(-2)])          # flow against
    assert not mtf.trigger_fires("long", 100.2, [_bar(+2)])         # traded through the level
    assert not mtf.trigger_fires("long", 99.5, [_bar(0)])


def test_short_trigger_mirrors_the_long():
    assert mtf.trigger_fires("short", 101.5, [_bar(-2)])
    assert not mtf.trigger_fires("short", 101.5, [_bar(+2)])
    assert not mtf.trigger_fires("short", 100.8, [_bar(-2)])


def test_trigger_reads_only_the_last_bar_and_needs_one():
    assert not mtf.trigger_fires("long", 99.5, [])
    assert mtf.trigger_fires("long", 99.5, [_bar(-5), _bar(+1)])
    assert not mtf.trigger_fires("long", 99.5, [_bar(+5), _bar(-1)])


# -- trigger in the graph --------------------------------------------------- #
def _nodes(monkeypatch, tmp_path, **kw):
    nodes = TradingNodes(Settings(mode=RunMode.SIM, llm_enabled=False, **kw),
                         TradeJournal(str(tmp_path / "j.db")), now_ts=0)
    intent = TradeIntent("BTCUSDT", "long", "liquidity_sweep_choch", 100, 99.5, 102, 103,
                         confluence_score=9, expected_rr=4.0)
    monkeypatch.setattr(nodes.strategy, "evaluate", lambda sig, ctx=None: intent)
    return nodes


def _state(trades):
    candle = Candle(timestamp=0, open=100, high=101, low=99.8, close=100.5, volume=10,
                    trades=trades)
    return {"candle": candle,
            "signal": SignalObject(timestamp=0, symbol="BTCUSDT", price=100.5, high=101, low=99.8)}


def test_trigger_off_passes_the_intent_through(monkeypatch, tmp_path):
    nodes = _nodes(monkeypatch, tmp_path)
    assert nodes.strategy_node(_state([]))["intent"] is not None


def test_trigger_on_confirms_with_buying_in_the_last_15s(monkeypatch, tmp_path):
    nodes = _nodes(monkeypatch, tmp_path, mtf_15s_trigger_enabled=True)
    ticks = [_t(10_000, 100.1, 1, sell=True), _t(50_000, 100.4, 3)]
    assert nodes.strategy_node(_state(ticks))["intent"] is not None


def test_trigger_on_drops_the_entry_when_the_last_15s_is_selling(monkeypatch, tmp_path):
    nodes = _nodes(monkeypatch, tmp_path, mtf_15s_trigger_enabled=True)
    ticks = [_t(10_000, 100.1, 5), _t(50_000, 100.4, 3, sell=True)]
    assert nodes.strategy_node(_state(ticks))["intent"] is None


def test_trigger_on_without_ticks_takes_no_trade(monkeypatch, tmp_path):
    """Klines have no ticks. The trigger does not guess: no ticks, no entry."""
    nodes = _nodes(monkeypatch, tmp_path, mtf_15s_trigger_enabled=True)
    assert nodes.strategy_node(_state([]))["intent"] is None


# -- research hypotheses ---------------------------------------------------- #
def test_hypotheses_are_declared_and_the_15s_one_is_blocked():
    assert CATALOG["mtf_bias_filter"].timeframe == "1m" and not CATALOG["mtf_bias_filter"].blocked
    assert not CATALOG["baseline_1m_setups"].blocked
    assert "trade-level" in CATALOG["mtf_15s_trigger"].blocked


def test_blocked_hypothesis_is_refused_without_a_registry_row(tmp_path, capsys):
    db = str(tmp_path / "j.db")
    assert cli.main(["run", "mtf_15s_trigger", "--db", db]) == 4
    assert "blocked" in capsys.readouterr().err
    cli.main(["registry", "--db", db])
    assert capsys.readouterr().out.startswith("0 rows")


def test_pipeline_samples_come_from_the_real_graph():
    """The scripted scenario produces one trade; its gross return is read back
    from the journal as a fraction of notional, before fees."""
    from maya.data.scenarios import ScenarioFeed

    candles = list(ScenarioFeed(Settings(mode=RunMode.SIM, scenario=True)).candles())
    samples = pipeline_samples({"BTCUSDT": candles})
    assert len(samples) == 1
    assert 0.03 < samples[0].gross_return < 0.06          # the scripted ~4.6% short
