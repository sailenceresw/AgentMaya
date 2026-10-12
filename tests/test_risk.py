"""Tests for the risk layer — the most safety-critical code (spec section 7)."""
import pytest

from maya.check_connection import (
    _extract_balance,
    _extract_orders,
    _extract_positions,
    _extract_price,
    _extract_server_time,
    run_diagnostics,
)
from maya.config import RiskLimits, RunMode, Settings
from maya.risk.manager import RiskManager, SystemState
from maya.risk.sizing import calculate_position_notional
from maya.strategy.models import TradeIntent


def _settings():
    return Settings(mode=RunMode.SIM, start_equity=1000)


def test_position_sizing_caps_at_notional_limit():
    # Tiny stop distance would imply huge notional -> must be capped at 5x equity.
    notional = calculate_position_notional(1000, 0.5, entry=100, sl=99.99)
    assert notional <= 1000 * RiskLimits.POSITION_NOTIONAL_CAP_MULT


def test_position_sizing_uses_fixed_usd_risk():
    notional = calculate_position_notional(1000, RiskLimits.MAX_RISK_PER_TRADE_USD, entry=100, sl=95)
    # risk = $20; sl distance = 5% => notional = 400
    assert abs(notional - 400) < 1e-6


def _intent(rr=5.0, take_profit_1=120, stop_loss=99):
    # A 1% stop. Anything much wider is unreachable at 20x: the position
    # liquidates first (see test_rejects_stop_beyond_liquidation).
    return TradeIntent("BTCUSDT", "long", "value_area_absorption",
                       entry_price=100, stop_loss=stop_loss, take_profit_1=take_profit_1,
                       take_profit_2=125, confluence_score=8, expected_rr=rr)


def test_rejects_low_roi():
    """min_roi_pct is screened on *margin*, so at 20x a 1% move to target is
    a 20% return and must still fail a 40% threshold."""
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000,
                              min_roi_pct=40.0, leverage=20), 1000)
    decision = rm.screen(_intent(rr=5.0, take_profit_1=101), now_ts=0)
    assert not decision.approved
    assert "on margin" in decision.reason


def test_allows_minimum_roi():
    # 3% move x 20x = 60% on margin, clears the 40% threshold.
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000,
                              min_roi_pct=40.0, leverage=20), 1000)
    decision = rm.screen(_intent(rr=5.0, take_profit_1=103, stop_loss=99.5), now_ts=0)
    assert decision.approved


def test_roi_gate_is_measured_on_margin_not_raw_price_move():
    """Regression guard for the bug that made the agent inert: comparing an
    unleveraged price move against a 40% threshold rejected every realistic
    1m scalp, because BTC does not move 40% intraday."""
    intent = _intent(rr=5.0, take_profit_1=104.64)  # ~4.64%, the scenario trade
    assert intent.expected_roi_pct < 5.0            # nowhere near 40 on its own
    assert intent.roi_on_margin_pct(20) > 90.0      # but 92.8% on margin at 20x

    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000,
                              min_roi_pct=40.0, leverage=20), 1000)
    assert rm.screen(intent, now_ts=0).approved


def test_same_trade_is_rejected_at_low_leverage():
    """The gate is leverage-dependent by design — the same setup that clears
    40% on margin at 20x does not at 5x."""
    intent = _intent(rr=5.0, take_profit_1=104.64)
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000,
                              min_roi_pct=40.0, leverage=5), 1000)
    assert not rm.screen(intent, now_ts=0).approved


def test_margin_sizing_gives_margin_times_leverage():
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000, leverage=20,
                              sizing_mode="margin", margin_per_trade_usd=20.0,
                              min_roi_pct=40.0), 1000)
    decision = rm.screen(_intent(rr=5.0, take_profit_1=103, stop_loss=99.5), now_ts=0)
    assert decision.approved
    assert decision.leverage == 20
    assert abs(decision.position_notional - 400.0) < 1e-6  # $20 x 20x


def test_margin_sizing_still_respects_the_per_trade_risk_ceiling():
    """Sizing by margin means the stop distance decides the loss, so a wide
    stop must be clamped by MAX_RISK_PER_TRADE_USD — otherwise a '$20 trade'
    could lose far more than $20."""
    from maya.risk.sizing import calculate_margin_notional

    # 50% stop: $20 x 20x = $400 notional would risk $200, way over the $20 cap.
    notional = calculate_margin_notional(1000, 20.0, 20, entry=100, sl=50)
    assert notional * 0.5 <= RiskLimits.MAX_RISK_PER_TRADE_USD + 1e-9


def test_risk_sizing_mode_still_available():
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000, leverage=20,
                              sizing_mode="risk", risk_per_trade_usd=20.0,
                              min_roi_pct=40.0), 1000)
    decision = rm.screen(_intent(rr=5.0, take_profit_1=103, stop_loss=99.5), now_ts=0)
    # 0.5% stop, $20 risk -> $4000 notional: derived from the stop, not margin.
    # (A 0.5% stop so the prices really are ~5:1 after fees, as rr=5.0 claims.)
    assert decision.approved
    assert abs(decision.position_notional - 4000.0) < 1e-6


def test_leverage_above_hard_limit_is_rejected_not_clamped():
    import pytest

    with pytest.raises(Exception):
        Settings(mode=RunMode.SIM, leverage=RiskLimits.MAX_LEVERAGE + 1)


def test_rejects_when_halted():
    rm = RiskManager(_settings(), 1000)
    rm.manual_halt()
    assert rm.screen(_intent(), now_ts=0).approved is False


def test_daily_loss_limit_triggers_halt():
    rm = RiskManager(_settings(), 1000)
    rm.account.equity = 1000 * (1 - 0.04)  # -4% > 3% daily limit
    rm.check_kill_switch(now_ts=0)
    assert rm.account.state is SystemState.HALTED


def test_max_one_position():
    rm = RiskManager(_settings(), 1000)
    rm.on_trade_opened()
    assert rm.screen(_intent(), now_ts=0).approved is False


def test_connection_extractors_handle_sdk_models():
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def data(self):
            return self._payload

    class FakeModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    server_time = FakeResponse({"serverTime": 1234})
    assert _extract_server_time(server_time) == 1234

    ticker = FakeResponse({"price": "99.5"})
    assert _extract_price(ticker) == 99.5

    balance = [FakeModel(asset="USDT", balance="10", withdrawAvailable="5")]
    b, a = _extract_balance(balance)
    assert b == 10.0
    assert a == 5.0

    positions = [FakeModel(symbol="BTCUSDT", positionAmt="1", positionSide="LONG")]
    extracted_positions = _extract_positions(positions)
    assert extracted_positions[0]["contracts"] == 1.0

    orders = [FakeModel(orderId=1, type="LIMIT", side="BUY")]
    extracted_orders = _extract_orders(orders)
    assert extracted_orders[0]["id"] == 1


def test_run_diagnostics_uses_supported_binance_methods(monkeypatch):
    class FakeExchange:
        def __init__(self):
            self.rest_api = self

        def check_server_time(self):
            return {"serverTime": 1_700_000_000_000}

        def symbol_price_ticker(self, symbol):
            return {"symbol": symbol, "price": "100.0"}

        def futures_account_balance_v3(self):
            return [{"asset": "USDT", "balance": "100", "withdrawAvailable": "100"}]

        def futures_account_configuration(self):
            return {"dualSidePosition": False}

        def position_information_v3(self, symbol):
            return [{"symbol": symbol, "positionAmt": "1", "positionSide": "LONG"}]

        def current_all_open_orders(self, symbol):
            return [{"orderId": 1, "type": "LIMIT", "side": "BUY"}]

    monkeypatch.setattr("maya.check_connection._build_exchange", lambda settings: FakeExchange())
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")

    report = run_diagnostics(Settings(mode=RunMode.TESTNET, start_equity=1000))

    assert report.last_price == 100.0
    assert report.open_positions[0]["contracts"] == 1.0
    assert report.open_orders[0]["id"] == 1


# -- periodic limit rollover ------------------------------------------------ #
# Regression: trades_today was only ever incremented and day/week_start_equity
# were only ever set at construction, which silently turned three *periodic*
# limits into lifetime ones — MAX_TRADES_PER_DAY became a cap of 5 trades for
# the life of the process, and the "daily"/"weekly" loss limits measured
# drawdown from session start forever.
_DAY = 86_400_000


def _spend_daily_trade_budget(rm, ts=0):
    for _ in range(RiskLimits.MAX_TRADES_PER_DAY):
        assert rm.screen(_intent(), ts).approved
        rm.on_trade_opened()
        rm.on_trade_closed(+1.0, ts)  # a win: no cooldown, no consecutive losses


def test_daily_trade_cap_resets_on_the_next_day():
    rm = RiskManager(_settings(), equity=1000)
    _spend_daily_trade_budget(rm)
    assert not rm.screen(_intent(), 0).approved  # budget spent today

    assert rm.screen(_intent(), _DAY).approved
    assert rm.account.trades_today == 0


def test_daily_trade_cap_is_not_a_lifetime_cap():
    """A month later the agent must still be allowed to trade."""
    rm = RiskManager(_settings(), equity=1000)
    _spend_daily_trade_budget(rm)
    assert rm.screen(_intent(), 30 * _DAY).approved


def test_day_start_equity_rolls_so_daily_loss_limit_is_daily():
    rm = RiskManager(_settings(), equity=1000)
    rm.on_trade_opened()
    rm.on_trade_closed(-25.0, 0)          # -2.5%: under the 3% daily limit
    assert rm.account.state is not SystemState.HALTED

    rm.check_kill_switch(_DAY)             # new day -> today's baseline is 975
    assert rm.account.day_start_equity == 975.0
    rm.on_trade_opened()
    rm.on_trade_closed(-25.0, _DAY)        # another -2.56% *of the new day*
    # Cumulative drawdown is now 5% from session start, but only ~2.6% today,
    # so a *daily* limit must not have fired.
    assert rm.account.state is not SystemState.HALTED


def test_period_anchor_does_not_reset_counters_restored_from_journal():
    """The first roll must anchor the period, not wipe restored state — and
    epoch day 0 is a legitimate anchor, not a 'never anchored' sentinel."""
    rm = RiskManager(_settings(), equity=1000)
    rm.account.trades_today = 3
    rm.check_kill_switch(0)
    assert rm.account.trades_today == 3


def test_rollover_does_not_resume_a_manually_halted_system():
    """A manual halt describes a condition a calendar change does not repair,
    so it must survive the rollover that clears a daily-loss halt."""
    rm = RiskManager(_settings(), equity=1000)
    rm.manual_halt()
    rm.check_kill_switch(_DAY)
    assert rm.account.state is SystemState.HALTED
    assert not rm.screen(_intent(), _DAY).approved


def test_daily_loss_halt_lifts_on_the_next_day():
    """A 'daily' limit that halts forever is a one-shot kill switch wearing
    the wrong name."""
    rm = RiskManager(_settings(), equity=1000)
    rm.check_kill_switch(0)                 # anchor the period
    rm.on_trade_opened()
    rm.on_trade_closed(-40.0, 0)            # -4%, past the 3% daily limit
    assert rm.account.state is SystemState.HALTED

    rm.check_kill_switch(_DAY)
    assert rm.account.state is SystemState.RUNNING
    assert rm.screen(_intent(), _DAY).approved


def test_equity_floor_halt_survives_the_day_rollover():
    """Below the floor is not a condition tomorrow fixes."""
    rm = RiskManager(_settings(), equity=1000)
    rm.check_kill_switch(0)
    rm.account.equity = RiskLimits.EQUITY_FLOOR_USDT - 1
    rm.check_kill_switch(0)
    assert rm.account.state is SystemState.HALTED
    rm.check_kill_switch(_DAY)
    assert rm.account.state is SystemState.HALTED


def test_a_system_halted_twice_stays_halted_after_one_period_clears():
    rm = RiskManager(_settings(), equity=1000)
    rm.check_kill_switch(0)
    rm.manual_halt()
    rm.on_trade_opened()
    rm.on_trade_closed(-40.0, 0)            # daily halt on top of the manual one
    rm.check_kill_switch(_DAY)              # clears only the daily one
    assert rm.account.state is SystemState.HALTED


def test_rejects_stop_beyond_liquidation():
    """At 20x an isolated position liquidates after roughly a 3.75% adverse
    move (5% initial margin less 1.25% maintenance). A 5% stop would never
    fire — the venue would liquidate first and keep the whole margin."""
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000,
                              min_roi_pct=10.0, leverage=20), 1000)
    decision = rm.screen(_intent(rr=5.0, take_profit_1=125, stop_loss=95), now_ts=0)
    assert not decision.approved
    assert "liquidation" in decision.reason


def test_same_wide_stop_is_fine_at_lower_leverage():
    # At 5x liquidation is ~18.75% away, so a 5% stop is well inside it.
    rm = RiskManager(Settings(mode=RunMode.SIM, start_equity=1000,
                              min_roi_pct=10.0, leverage=5), 1000)
    assert rm.screen(_intent(rr=5.0, take_profit_1=125, stop_loss=95), now_ts=0).approved


def test_liquidation_distance_estimate():
    from maya.risk.sizing import liquidation_distance_frac

    assert liquidation_distance_frac(20, 0.0125) == pytest.approx(0.0375)
    assert liquidation_distance_frac(1, 0.0125) == pytest.approx(0.9875)


# -- fee-aware gate ---------------------------------------------------------- #
def _fee_rm(**kw):
    from maya.config import Venue

    return RiskManager(Settings(mode=RunMode.SIM, venue=Venue.HYPERLIQUID, start_equity=1000,
                                leverage=20, min_roi_pct=10.0, **kw), 1000)


def test_fee_gate_rejects_a_scalp_that_only_pays_the_fees():
    """The classic 1m scalp: 0.3% stop, 0.5% target. Gross it is 1.7:1; after
    Hyperliquid + MetaMask's builder fee (0.26% round trip) the win is 0.24%
    and the loss 0.56% — it needs a 70% win rate just to break even."""
    rm = _fee_rm()
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption", entry_price=100,
                         stop_loss=99.7, take_profit_1=100.5, take_profit_2=101,
                         confluence_score=8, expected_rr=5.0)
    decision = rm.screen(intent, now_ts=0)
    assert not decision.approved
    assert "fees" in decision.reason


def test_fee_gate_passes_a_trade_with_room_for_the_round_trip():
    rm = _fee_rm()
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption", entry_price=100,
                         stop_loss=99.5, take_profit_1=103, take_profit_2=104,
                         confluence_score=8, expected_rr=6.0)
    assert rm.screen(intent, now_ts=0).approved


def test_fee_gate_uses_the_calibrated_builder_fee():
    """With MetaMask's real builder fee below the cap, a marginal trade can
    pass that the conservative default would refuse."""
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption", entry_price=100,
                         stop_loss=99.6, take_profit_1=101.6, take_profit_2=102,
                         confluence_score=8, expected_rr=4.0)
    assert not _fee_rm().screen(intent, now_ts=0).approved
    assert _fee_rm(builder_fee_bps=0.0).screen(intent, now_ts=0).approved


def test_roi_gate_is_net_of_fees():
    # 0.6% move x 20 = 12% gross on margin, but only 6.8% after a 0.26% round trip.
    rm = _fee_rm(max_fee_share=1.0, min_net_rr=0.0)
    intent = TradeIntent("BTCUSDT", "long", "value_area_absorption", entry_price=100,
                         stop_loss=99.9, take_profit_1=100.6, take_profit_2=101,
                         confluence_score=8, expected_rr=6.0)
    decision = rm.screen(intent, now_ts=0)
    assert not decision.approved and "after fees" in decision.reason


def test_blank_fee_settings_mean_use_the_default():
    s = Settings(fee_maker_bps="", builder_fee_bps="  ", fee_rate="")
    assert s.fee_maker_bps is None and s.builder_fee_bps is None and s.fee_rate is None
