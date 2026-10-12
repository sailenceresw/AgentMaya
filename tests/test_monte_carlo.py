"""Tests for the Monte Carlo daily-return simulator — checks the fee math
against the real FEE_RATE constant, pins the sizing/leverage semantics, and
sanity-checks the distribution shape with a fixed seed (deterministic, not
flaky)."""
import statistics

import pytest

from maya.backtest.monte_carlo import (
    SimParams,
    fee_pct_of_equity,
    simulate,
    simulate_detailed,
    summarize,
)
from maya.execution.engine import FEE_RATE


def test_fee_pct_matches_hand_calculation():
    # risk=0.5%, stop=0.5% -> notional = 100% of equity -> fee = 1.0 * FEE_RATE * 2 * 100
    fee = fee_pct_of_equity(risk_pct=0.5, avg_stop_distance_pct=0.5)
    expected = 1.0 * FEE_RATE * 2 * 100
    assert abs(fee - expected) < 1e-9


def test_tighter_stop_means_bigger_notional_means_more_fee_drag():
    tight = fee_pct_of_equity(risk_pct=0.5, avg_stop_distance_pct=0.3)
    wide = fee_pct_of_equity(risk_pct=0.5, avg_stop_distance_pct=2.0)
    assert tight > wide


def test_simulate_is_deterministic_with_a_fixed_seed():
    p = SimParams(simulations=50, days=50, seed=7)
    a = simulate(p)
    b = simulate(p)
    assert a == b


def test_higher_win_rate_produces_higher_mean_daily_return():
    low = simulate(SimParams(win_rate=0.30, simulations=300, days=50, seed=1))
    high = simulate(SimParams(win_rate=0.55, simulations=300, days=50, seed=1))
    assert summarize(high)["mean_daily_return_pct"] > summarize(low)["mean_daily_return_pct"]

def _pct_weeks_hitting(params: SimParams, target_pct: float) -> float:
    """Share of simulated *weeks* clearing a target.

    The headline unit is now a day, but these three tests pin a sizing
    regression whose signature was always stated weekly, so they read
    ``weekly_returns`` directly rather than restate the thresholds in daily
    terms and lose the link to the bug they guard.
    """
    weeks = simulate_detailed(params).weekly_returns
    return sum(1 for w in weeks if w >= target_pct) / len(weeks) * 100


def test_realistic_assumptions_rarely_hit_40pct_in_a_week():
    """A 40%/week target is rare but NOT the ~0% the old model reported.

    That old figure came from simulating 0.5% risk per trade
    (RiskLimits.MAX_RISK_PER_TRADE_PCT, which nothing in the live system
    uses). The risk manager actually risks settings.risk_per_trade_usd — $20,
    or 2% of the $1,000 default equity, four times as much.

    At real sizing one win is +7.71% of equity, so five straight wins is
    +45%: a 40% week needs a good run of ~5 trades, not a miracle. It lands
    around 6% of weeks — roughly 1 in 17.
    """
    p = SimParams(win_rate=0.45, trades_per_day_mean=1.6, simulations=2000, days=50, seed=3,
                  risk_model="fixed_usd", risk_usd=20.0)
    hit_rate = _pct_weeks_hitting(p, 40.0)
    assert 3.0 < hit_rate < 10.0, hit_rate


def test_legacy_half_percent_risk_reproduces_the_old_near_zero_result():
    """Pins the cause of the change above: switch the risk model back to the
    legacy 0.5%-of-equity reading and the 'essentially never' result returns.
    Guards against anyone silently reverting the sizing fix."""
    p = SimParams(win_rate=0.45, trades_per_day_mean=1.6, simulations=2000, days=50,
                  seed=3, risk_model="fixed_pct", risk_pct=0.5)
    assert _pct_weeks_hitting(p, 40.0) < 0.1


def test_maxed_out_best_case_reaches_40pct_a_meaningful_share_of_weeks():
    # Absolute ceiling: 49% win rate (top of the spec's realistic range) AND
    # maxed out at the hard trade-frequency cap every week.
    p = SimParams(win_rate=0.49, trades_per_day_mean=5.0, max_trades_per_day=5,
                 simulations=2000, days=50, seed=5,
                 risk_model="fixed_usd", risk_usd=20.0)
    assert _pct_weeks_hitting(p, 40.0) > 5.0


def test_daily_target_is_optional_and_reported_when_given():
    """The first question is what the architecture *can* do, so summarize()
    must not require a target — but must evaluate one when asked."""
    p = SimParams(simulations=400, days=50, seed=13)
    returns = simulate(p)
    assert not any("target" in k for k in summarize(returns))
    assert "pct_days_hitting_5pct_target" in summarize(returns, target_pct=5.0)


def test_days_are_trimmed_to_exactly_the_number_requested():
    """`weeks` rounds up to cover `days`, so a non-multiple-of-5 request must
    not silently report a larger sample than it was asked for."""
    p = SimParams(simulations=10, days=7, seed=19)
    assert summarize(simulate(p))["n_days_simulated"] == 7 * 10


def test_risk_limits_materially_reduce_trade_count():
    """The halts/cooldown are not decoration — with them off, more trades get
    taken and the loss tail is fatter."""
    from maya.backtest.monte_carlo import simulate_detailed

    guarded = simulate_detailed(SimParams(simulations=400, days=50, seed=17))
    unguarded = simulate_detailed(
        SimParams(simulations=400, days=50, seed=17, enforce_risk_limits=False))

    assert unguarded.mean_trades_per_day > guarded.mean_trades_per_day
    assert min(unguarded.weekly_returns) < min(guarded.weekly_returns)


def test_cooldown_persists_across_days_until_a_win():
    """RiskManager.on_trade_closed resets consecutive_losses only on a win, so
    a run stuck at 2+ losses keeps re-arming the cooldown day after day. A
    losing-heavy configuration should therefore end a large share of days."""
    from maya.backtest.monte_carlo import simulate_detailed

    result = simulate_detailed(
        SimParams(win_rate=0.20, trades_per_day_mean=3.0, simulations=400, days=50, seed=23))
    assert result.pct_days_ended_by_cooldown > 20.0


def test_notional_cap_limits_risk_at_low_equity():
    """POSITION_NOTIONAL_CAP_MULT (5x equity) caps risk at
    5 * stop_distance of equity, so a tiny account cannot risk the full fixed
    USD amount. With $100 equity and a 0.7% stop that ceiling is 3.5%, well
    under the $20 (20%) the fixed-USD setting would otherwise take."""
    from maya.backtest.monte_carlo import simulate_detailed

    tiny = simulate_detailed(SimParams(start_equity=100.0, risk_usd=20.0,
                                       simulations=300, days=20, seed=31))
    # If the cap were ignored, single trades would swing ~20% and the worst
    # week would blow far past the weekly kill switch.
    assert min(tiny.weekly_returns) > -25.0


def test_uncompounded_result_is_independent_of_day_count():
    """The default (non-compounding) mode must answer the same question
    regardless of how many days are requested — that is the whole point of
    not compounding fixed-USD risk."""
    short = summarize(simulate(SimParams(simulations=800, days=25, seed=41)))
    long = summarize(simulate(SimParams(simulations=800, days=200, seed=41)))
    assert abs(short["mean_daily_return_pct"] - long["mean_daily_return_pct"]) < 0.5


def test_leverage_does_not_change_pnl_when_risk_is_fixed():
    """The single most misunderstood thing about leverage: with a fixed stop
    and a fixed risk amount, position size is set by the stop distance, so
    5x and 20x produce *identical* P&L. Leverage only changes margin locked
    and where liquidation sits."""
    kw = dict(simulations=600, days=40, seed=77, risk_model="fixed_usd", risk_usd=20.0)
    low = summarize(simulate(SimParams(leverage=5, **kw)))
    high = summarize(simulate(SimParams(leverage=20, **kw)))
    assert low["mean_daily_return_pct"] == high["mean_daily_return_pct"]
    assert low["worst_day_pct"] == high["worst_day_pct"]


def test_leverage_only_changes_margin_and_liquidation_distance():
    kw = dict(risk_model="fixed_usd", risk_usd=20.0)
    low, high = SimParams(leverage=5, **kw), SimParams(leverage=20, **kw)
    # Same position, four times less margin tied up.
    assert low.notional_at(1000.0) == high.notional_at(1000.0)
    assert high.initial_margin_usd == pytest.approx(low.initial_margin_usd / 4)
    # ...and liquidation sits four times closer.
    assert high.liquidation_move_frac < low.liquidation_move_frac
    # At a 0.7% stop and 20x, liquidation (~4.5%) is still far beyond the stop.
    assert high.stop_to_liquidation_ratio < 0.25


def test_leverage_DOES_change_size_in_margin_mode():
    """The complement of the test above, and the reason the distinction
    matters: when you size by margin, leverage *is* your position size."""
    kw = dict(risk_model="fixed_margin", margin_usd=20.0)
    low, high = SimParams(leverage=5, **kw), SimParams(leverage=20, **kw)
    assert high.notional_at(1000.0) == pytest.approx(low.notional_at(1000.0) * 4)
    assert high.initial_risk_pct == pytest.approx(low.initial_risk_pct * 4)


def test_simulator_defaults_track_live_settings():
    """The drift that caused the original bug — the simulator modelling one
    thing while the live risk manager did another — must not recur."""
    from maya.config import Settings

    s, p = Settings(), SimParams()
    assert p.leverage == s.leverage
    assert p.start_equity == s.start_equity
    assert p.risk_model == ("fixed_margin" if s.sizing_mode == "margin" else "fixed_usd")
    assert p.margin_usd == s.margin_per_trade_usd


def test_fixed_margin_sizes_notional_as_margin_times_leverage():
    p = SimParams(risk_model="fixed_margin", margin_usd=20.0, leverage=20)
    assert p.notional_at(1000.0) == pytest.approx(400.0)
    # Risk is then whatever the stop implies, not a number you chose.
    assert p.initial_risk_pct == pytest.approx(400.0 * 0.007 / 1000 * 100)


def test_fixed_margin_risks_far_less_than_fixed_usd_at_the_same_leverage():
    """$20 of margin at 20x and $20 of risk at 20x are not the same trade —
    they differ by ~7x in position size. This is the ambiguity that has to be
    resolved before any of these numbers mean anything."""
    margin_mode = SimParams(risk_model="fixed_margin", margin_usd=20.0, leverage=20)
    risk_mode = SimParams(risk_model="fixed_usd", risk_usd=20.0, leverage=20)
    assert risk_mode.notional_at(1000.0) > margin_mode.notional_at(1000.0) * 5


def test_stop_wider_than_liquidation_buffer_forfeits_the_whole_margin():
    """At 50x the buffer is ~1.5%; a 3% stop means the position is wiped
    before the stop can fill, so a loss costs the full margin."""
    from maya.backtest.monte_carlo import simulate_detailed

    p = SimParams(leverage=50, avg_stop_distance_pct=3.0, win_rate=0.0,
                  simulations=200, days=10, seed=91)
    assert p.stop_to_liquidation_ratio > 1.0
    result = simulate_detailed(p)
    # Every trade loses; losses are margin-sized, so weeks are deeply negative.
    assert min(result.weekly_returns) < 0


def test_costs_raise_the_breakeven_win_rate_above_the_frictionless_floor():
    from maya.backtest.monte_carlo import _breakeven_win_rate

    frictionless = SimParams(slippage_bps=0.0, funding_rate_hourly=0.0, fee_rate=0.0)
    realistic = SimParams(slippage_bps=2.0, funding_rate_hourly=0.0000693)
    assert _breakeven_win_rate(realistic) > _breakeven_win_rate(frictionless)
    # At 1:4 R:R the zero-cost floor is exactly 20%.
    assert _breakeven_win_rate(frictionless) == pytest.approx(20.0)
    assert 20.0 < _breakeven_win_rate(realistic) < 30.0


def test_fee_rate_follows_the_venue():
    """Per-side average of the worst round trip, venue by venue.

    Hyperliquid: limit entry (maker 1.5bps) + trigger-market exit (taker
    4.5bps), plus MetaMask's builder fee on both legs (10bps cap until the
    quote reports the real one) = 26bps round trip = 13bps per side. The old
    figure, 1.5bps per side, was the maker rate alone and understated the cost
    of a Hyperliquid trade through MetaMask about 9x.
    Binance: limit entry (maker 2bps) + stop-market exit (taker 5bps) = 7bps.
    """
    from maya.config import RunMode, Settings, Venue

    hl = Settings(mode=RunMode.TESTNET, venue=Venue.HYPERLIQUID)
    bn = Settings(mode=RunMode.TESTNET, venue=Venue.BINANCE)
    assert hl.effective_fee_rate == pytest.approx(0.0013)
    assert bn.effective_fee_rate == pytest.approx(0.00035)
    assert Settings(venue=Venue.HYPERLIQUID, builder_fee_bps=0.0).effective_fee_rate \
        == pytest.approx(0.0003)
    # An explicit override still wins over the venue default.
    assert Settings(venue=Venue.HYPERLIQUID, fee_rate=0.001).effective_fee_rate == 0.001


def test_tighter_stops_are_eaten_by_costs():
    """Cost as a share of risk scales inversely with stop distance — the
    reason very tight stops lose to fees regardless of win rate."""
    tight = SimParams(avg_stop_distance_pct=0.1)
    wide = SimParams(avg_stop_distance_pct=2.0)
    assert tight.cost_pct_of_risk > wide.cost_pct_of_risk * 5


def test_compounding_grows_equity_over_a_year():
    from maya.backtest.monte_carlo import simulate_detailed

    result = simulate_detailed(SimParams(simulations=400, days=260, seed=43, compound=True))
    assert statistics.median(result.final_equities) > SimParams().start_equity


def test_summary_percentiles_are_ordered():
    p = SimParams(simulations=500, days=50, seed=11)
    summary = summarize(simulate(p))
    assert summary["p5_daily_return_pct"] <= summary["p25_daily_return_pct"]
    assert summary["p25_daily_return_pct"] <= summary["median_daily_return_pct"]
    assert summary["median_daily_return_pct"] <= summary["p75_daily_return_pct"]
    assert summary["p75_daily_return_pct"] <= summary["p95_daily_return_pct"]
