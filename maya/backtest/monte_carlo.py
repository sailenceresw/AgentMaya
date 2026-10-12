"""Monte Carlo daily-return simulator.

Answers one question with numbers instead of argument: **given Maya's own risk
architecture, what daily returns are actually reachable?**

The reported unit is a *day*, which is the cadence the system actually runs at:
a 1-minute strategy with a 5-trades-per-day cap and a 3% daily-loss halt. The
simulation still advances a week at a time internally, because the 8% weekly
kill switch is a real limit that can only bind across days — so a day here is a
day inside a week that has its own history, not an isolated coin flip.

The model is driven by the parameters the live system really uses — the
fixed-USD risk amount from ``Settings.risk_per_trade_usd``, the notional cap
and loss limits from ``RiskLimits``, and the venue's fee model
(:mod:`maya.fees`) the execution engine charges.

**What is modeled** (all of it enforced per simulated day, not approximated):

- Fixed-USD position sizing. This is the important one: risking a *fixed
  dollar amount* means risk-as-%-of-equity **rises as you lose and falls as
  you win**, which is a materially different (and more dangerous) profile
  than fixed-percent risk. At the $20 / $1,000 defaults that is 2% per trade,
  not the 0.5% a fixed-percent reading would suggest.
- The ``POSITION_NOTIONAL_CAP_MULT`` (5x equity) cap, which starts binding at
  low equity and quietly *reduces* real risk there.
- The 3% daily-loss halt, the 2-consecutive-loss cooldown, the 8% weekly kill
  switch, ``MAX_TRADES_PER_DAY`` and the equity floor. At real sizing these
  bind constantly — two losing trades is already 4% of a $1,000 account, over
  the 3% daily limit — so a model that skipped them (as this one previously
  did) would not describe the system at all.

**Simplifications, stated plainly:**

- Win/loss outcomes are i.i.d. per trade. Real markets cluster into regimes,
  so genuine variance is very likely higher than this shows.
- Every win takes the full R:R target and every loss the full stop. Partial
  exits, breakeven stop-outs and max-duration force-closes are not modeled.
- The 60-minute cooldown is modeled as "no further trades that day". On a 1m
  strategy taking ~1-2 trades a day, an hour usually does consume the rest of
  the cluster, but this is an approximation in both directions.
- Trade *opportunities* per day are Poisson-sampled around a configurable
  mean; the strategy's real arrival process is not Poisson.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from maya.config import RiskLimits, Settings

TRADING_DAYS_PER_WEEK = 5


@dataclass
class SimParams:
    """Simulation inputs. Defaults mirror the live system's own configuration."""

    win_rate: float = 0.45  # spec's realistic 43-49% target, midpoint
    reward_risk: float = RiskLimits.MIN_RR_FOR_ENTRY  # 4.0 — the hard floor
    avg_stop_distance_pct: float = 0.7  # typical SL distance, % of price
    trades_per_day_mean: float = 1.6  # selective, not maxed out every day
    max_trades_per_day: int = RiskLimits.MAX_TRADES_PER_DAY
    days: int = 252  # a trading year
    simulations: int = 5_000
    seed: int = 42

    # -- risk model ------------------------------------------------------- #
    # How the position is sized. These are NOT interchangeable and the
    # difference is the single biggest driver of the result:
    #
    #   "fixed_usd"   risk a fixed $ amount to the stop. What
    #                 maya/risk/manager.py actually does today (it passes
    #                 settings.risk_per_trade_usd to calculate_position_notional).
    #                 Position size is derived from the stop distance, so
    #                 leverage does not affect P&L — only margin locked.
    #   "fixed_margin" commit a fixed $ amount of margin at `leverage`, giving
    #                 notional = margin x leverage. This is what most people
    #                 mean by "I use $20 at 20x". Risk then depends on the stop
    #                 distance rather than being chosen.
    #   "fixed_pct"   legacy RiskLimits.MAX_RISK_PER_TRADE_PCT reading, kept
    #                 only to reproduce old results. Nothing live uses it.
    # Defaults are read from Settings so this simulator cannot silently drift
    # out of sync with what the live risk manager actually does — the drift
    # that previously made it model 0.5% risk while the system used 2%.
    risk_model: str = field(
        default_factory=lambda: "fixed_margin" if Settings().sizing_mode == "margin"
        else "fixed_usd")
    risk_usd: float = field(default_factory=lambda: Settings().risk_per_trade_usd)
    margin_usd: float = field(default_factory=lambda: Settings().margin_per_trade_usd)
    start_equity: float = field(default_factory=lambda: Settings().start_equity)
    risk_pct: float = RiskLimits.MAX_RISK_PER_TRADE_PCT  # only for fixed_pct

    # -- leverage & the costs that scale with notional --------------------- #
    # Leverage never changes P&L for a given notional; it sets margin locked
    # and how far away liquidation sits. It matters here because (a) in
    # fixed_margin mode it determines notional, and (b) it decides whether a
    # stop-out or a liquidation comes first.
    leverage: int = field(default_factory=lambda: Settings().leverage)
    maintenance_margin_rate: float = 0.005  # ~0.5%, typical BTC perp lowest tier
    # Slippage charged on entry AND exit, in bps of notional. A 1m scalper
    # crossing the spread pays this every single trade; at tight stops it is
    # a meaningful share of risk.
    slippage_bps: float = 2.0
    # Per-side fee rate, resolved from the venue like everything else here.
    # Per-side average of the venue's worst-case round trip (maya.fees):
    # 13bps through MetaMask on Hyperliquid (exchange + builder fee), 3.5bps
    # on Binance. Using the wrong venue misstates cost drag several-fold.
    fee_rate: float = field(default_factory=lambda: Settings().effective_fee_rate)
    # Hyperliquid charges funding **hourly**, not every 8 hours. The default is
    # the rate actually observed on BTC testnet (-0.0000693105/h, taken as a
    # magnitude — the sign depends on which side you are on).
    funding_rate_hourly: float = 0.0000693
    avg_hold_hours: float = 0.5

    # Whether to enforce the halts/cooldowns. Off reproduces the old
    # (unrealistically smooth) behaviour; on is the default because at real
    # sizing these dominate the outcome.
    enforce_risk_limits: bool = True

    # Carry equity from one simulated week into the next.
    #
    # Off by default, and the reason matters. With *fixed-USD* risk, risk as a
    # percentage of equity falls as the account grows. Compounding a year of
    # positive expectancy therefore fills the sample with large-equity days
    # where $20 is a rounding error, and the return distribution silently
    # becomes a function of how many days you asked for. Since the question
    # this module exists to answer is "what does a day look like from where I
    # am now", equity is reset to ``start_equity`` every week, which makes the
    # result independent of ``days``.
    #
    # The reset is weekly rather than daily on purpose: resetting every day
    # would make each day an independent coin flip and the 8% weekly kill
    # switch could never bind, quietly deleting a real limit from the model.
    # Days within a week stay path-dependent, so a bad Monday still shapes
    # Tuesday.
    #
    # Turn it on for the different question — "what might a year do" —
    # where the compounding path and its drawdowns are the whole point.
    compound: bool = False

    @property
    def weeks(self) -> int:
        """Simulated weeks needed to cover ``days``. The engine advances a week
        at a time so the weekly kill switch has a period to bind over."""
        return -(-self.days // TRADING_DAYS_PER_WEEK)  # ceil

    @property
    def stop_frac(self) -> float:
        return self.avg_stop_distance_pct / 100

    @property
    def liquidation_move_frac(self) -> float:
        """Adverse price move that wipes the margin, as a fraction.

        Isolated margin: you are liquidated once losses eat the posted margin
        down to the maintenance requirement, i.e. at roughly
        ``1/leverage - maintenance_margin_rate`` of adverse movement.
        """
        return max(1e-9, 1.0 / self.leverage - self.maintenance_margin_rate)

    @property
    def stop_to_liquidation_ratio(self) -> float:
        """How much of the liquidation buffer the stop consumes. Above 1.0
        means liquidation happens BEFORE the stop — catastrophic."""
        return self.stop_frac / self.liquidation_move_frac

    def notional_at(self, equity: float) -> float:
        """Position notional for a given equity, mirroring maya/risk/sizing.py
        plus the constraints leverage imposes."""
        if self.risk_model == "fixed_margin":
            wanted = self.margin_usd * self.leverage
        elif self.risk_model == "fixed_pct":
            wanted = equity * (self.risk_pct / 100) / self.stop_frac
        else:
            wanted = self.risk_usd / self.stop_frac
        # You cannot control more notional than your margin supports, and the
        # spec's notional cap applies on top.
        return min(wanted,
                   equity * RiskLimits.POSITION_NOTIONAL_CAP_MULT,
                   equity * self.leverage)

    @property
    def cost_frac_of_notional(self) -> float:
        """Round-trip cost as a fraction of notional: fees + slippage + funding."""
        return (self.fee_rate * 2
                + (self.slippage_bps / 10_000) * 2
                + self.funding_rate_hourly * self.avg_hold_hours)

    @property
    def initial_risk_pct(self) -> float:
        """Risk per trade as % of *starting* equity — the headline number."""
        if not self.start_equity:
            return 0.0
        notional = self.notional_at(self.start_equity)
        return notional * self.stop_frac / self.start_equity * 100

    @property
    def initial_margin_usd(self) -> float:
        return self.notional_at(self.start_equity) / self.leverage

    @property
    def cost_pct_of_risk(self) -> float:
        """Round-trip cost as % of the amount risked. This is the number that
        kills tight-stop scalping: it is independent of position size and
        rises as the stop tightens."""
        risk_frac = self.stop_frac
        return self.cost_frac_of_notional / risk_frac * 100 if risk_frac else 0.0


@dataclass
class SimResult:
    daily_returns: list[float]
    # Retained because the 8% kill switch is a weekly limit, so judging it
    # needs a weekly view even though the headline unit is now a day.
    weekly_returns: list[float]
    final_equities: list[float]
    # Diagnostics: how often the risk architecture actually intervened.
    pct_days_halted_daily_loss: float
    pct_days_ended_by_cooldown: float
    pct_weeks_halted_weekly_loss: float
    pct_sims_hitting_equity_floor: float
    mean_trades_per_day: float


def fee_pct_of_equity(risk_pct: float, avg_stop_distance_pct: float,
                      fee_rate: float | None = None) -> float:
    """Round-trip fee cost as % of equity for one trade, derived from the real
    position-sizing formula (notional = risk / stop_distance) and the venue's
    per-side fee rate (``Settings().effective_fee_rate`` unless given)."""
    rate = Settings().effective_fee_rate if fee_rate is None else fee_rate
    notional_pct_of_equity = (risk_pct / 100) / (avg_stop_distance_pct / 100)
    return notional_pct_of_equity * rate * 2 * 100


def simulate_detailed(params: SimParams) -> SimResult:
    """Run the full path-dependent simulation.

    Vectorized across simulations: the loop is over weeks/days/trade-slots (a
    fixed, small number of iterations) while every simulation advances together
    as a numpy array. That keeps the per-trade branching — halts, cooldowns,
    the notional cap — exact rather than approximated, instead of the
    closed-form shortcut a non-path-dependent model could use.

    Randomness is drawn one week at a time rather than per trade-slot: same
    number of samples, ~25x fewer RNG calls, and bounded memory regardless of
    ``simulations``.
    """
    valid_models = ("fixed_usd", "fixed_margin", "fixed_pct")
    if params.risk_model not in valid_models:
        raise ValueError(f"unknown risk_model {params.risk_model!r} (expected one of {valid_models})")
    if params.leverage < 1:
        raise ValueError("leverage must be >= 1")

    rng = np.random.default_rng(params.seed)
    n = params.simulations
    n_weeks = params.weeks
    stop_frac = params.avg_stop_distance_pct / 100
    slots_per_day = min(RiskLimits.MAX_TRADES_PER_DAY, max(1, params.max_trades_per_day))
    opp_lambda = params.trades_per_day_mean

    equity = np.full(n, float(params.start_equity))
    weekly_returns = np.empty((n_weeks, n), dtype=float)
    daily_returns = np.empty((n_weeks, TRADING_DAYS_PER_WEEK, n), dtype=float)
    # Persists for the whole run, deliberately: RiskManager.on_trade_closed
    # resets this counter *only on a win* — never when the cooldown expires
    # and never at a day boundary. So once a run is at 2+ consecutive losses,
    # every further loss re-arms the cooldown until a win finally lands.
    consec_losses = np.zeros(n, dtype=np.int16)

    days_total = 0
    days_halted_daily = 0
    days_ended_cooldown = 0
    weeks_halted_weekly = 0
    trades_taken = 0
    floor_hit = np.zeros(n, dtype=bool)

    # Pre-computed constants, hoisted out of the hot loop.
    cap_mult = RiskLimits.POSITION_NOTIONAL_CAP_MULT
    cost_mult = params.cost_frac_of_notional  # fees + slippage + funding
    daily_limit = RiskLimits.DAILY_LOSS_LIMIT_PCT / 100
    weekly_limit = RiskLimits.WEEKLY_LOSS_LIMIT_PCT / 100
    floor = RiskLimits.EQUITY_FLOOR_USDT
    lev = params.leverage
    # A stop wider than the liquidation buffer means the position is wiped
    # before the stop can ever fill — the loss is the whole margin, not the
    # intended risk. At 20x the buffer is ~4.5%, so a 0.7% stop is safe; this
    # exists so the model tells the truth if that stops being true.
    liq_frac = params.liquidation_move_frac
    stop_beyond_liquidation = stop_frac >= liq_frac

    # Notional is constant per-equity except through the caps, so precompute
    # the uncapped target once.
    if params.risk_model == "fixed_margin":
        target_notional = params.margin_usd * lev
    elif params.risk_model == "fixed_usd":
        target_notional = params.risk_usd / stop_frac
    else:
        target_notional = None  # equity-dependent, computed in the loop

    # Scratch buffers reused every slot so the inner loop allocates nothing.
    notional = np.empty(n)
    pnl = np.empty(n)

    for week in range(n_weeks):
        if not params.compound:
            # Each week is an independent look at the account as it stands
            # today, so the sample is not dominated by grown-equity weeks.
            equity.fill(float(params.start_equity))
            consec_losses.fill(0)
        week_start = equity.copy()
        week_blocked = np.zeros(n, dtype=bool)

        # One RNG call per week instead of one per trade-slot.
        opportunities = np.minimum(
            rng.poisson(opp_lambda, (TRADING_DAYS_PER_WEEK, n)), slots_per_day)
        outcomes = rng.random((TRADING_DAYS_PER_WEEK, slots_per_day, n))

        for day in range(TRADING_DAYS_PER_WEEK):
            # trades_today and day_start_equity reset each day (spec 7.1);
            # consec_losses deliberately does not — see above.
            day_start = equity.copy()
            day_blocked = week_blocked.copy()
            day_opportunities = opportunities[day]

            for slot in range(slots_per_day):
                eligible = ~day_blocked & (slot < day_opportunities) & (equity > floor)
                if not eligible.any():
                    break  # slots only get less eligible; nothing left this day

                # -- sizing, mirroring maya/risk/sizing.py plus the limits
                #    leverage imposes (you cannot control more notional than
                #    your margin supports).
                if target_notional is not None:
                    np.multiply(equity, cap_mult, out=notional)
                    np.minimum(notional, target_notional, out=notional)
                else:
                    np.multiply(equity, params.risk_pct / 100 / stop_frac, out=notional)
                    np.minimum(notional, equity * cap_mult, out=notional)
                np.minimum(notional, equity * lev, out=notional)

                # Caps can bite, so recover the risk actually taken.
                real_risk = notional * stop_frac
                costs = notional * cost_mult

                won = outcomes[day, slot] < params.win_rate
                if stop_beyond_liquidation:
                    # Liquidation lands first: a loss forfeits the entire
                    # posted margin rather than the intended stop amount.
                    loss = -np.minimum(notional / lev, equity)
                else:
                    loss = -real_risk
                np.subtract(np.where(won, real_risk * params.reward_risk, loss),
                            costs, out=pnl)

                np.add(equity, pnl, out=equity, where=eligible)
                trades_taken += int(eligible.sum())

                # Consecutive losses reset on a win, increment on a loss, and
                # are untouched for sims that did not trade this slot.
                consec_losses = np.where(eligible, np.where(won, 0, consec_losses + 1),
                                         consec_losses)

                if params.enforce_risk_limits:
                    # 2 consecutive losses -> 60-minute cooldown, modeled as
                    # ending the trading day.
                    cooled = eligible & (consec_losses >= RiskLimits.MAX_CONSECUTIVE_LOSSES)
                    # 3% daily loss limit, 8% weekly kill switch. Compared
                    # without dividing — same test, one less array op.
                    halted = eligible & ((day_start - equity) >= day_start * daily_limit)
                    killed = eligible & ((week_start - equity) >= week_start * weekly_limit) \
                        & ~week_blocked

                    days_ended_cooldown += int((cooled & ~day_blocked).sum())
                    days_halted_daily += int((halted & ~day_blocked).sum())
                    weeks_halted_weekly += int(killed.sum())

                    week_blocked |= killed
                    day_blocked |= cooled | halted | week_blocked

                floor_hit |= equity <= floor

            # The day's return, measured against the equity it opened with —
            # the same baseline RiskManager.day_start_equity uses.
            np.divide(equity, day_start, out=pnl)
            daily_returns[week, day] = (pnl - 1) * 100
            days_total += n

        np.divide(equity, week_start, out=pnl)
        weekly_returns[week] = (pnl - 1) * 100

    total_weeks = n_weeks * n
    # Trim to exactly the days requested: `weeks` rounds up, so a `days` that
    # is not a multiple of 5 would otherwise report a slightly larger sample.
    daily_flat = daily_returns.reshape(n_weeks * TRADING_DAYS_PER_WEEK, n)[:params.days]
    return SimResult(
        daily_returns=daily_flat.ravel(order="C").tolist(),
        weekly_returns=weekly_returns.ravel(order="C").tolist(),
        final_equities=equity.tolist(),
        pct_days_halted_daily_loss=days_halted_daily / days_total * 100 if days_total else 0.0,
        pct_days_ended_by_cooldown=days_ended_cooldown / days_total * 100 if days_total else 0.0,
        pct_weeks_halted_weekly_loss=weeks_halted_weekly / total_weeks * 100 if total_weeks else 0.0,
        pct_sims_hitting_equity_floor=float(floor_hit.mean() * 100),
        mean_trades_per_day=trades_taken / days_total if days_total else 0.0,
    )


def simulate(params: SimParams) -> list[float]:
    """Daily % returns across every simulated day — the full empirical
    distribution, not just a mean. Kept as the module's stable entry point."""
    return simulate_detailed(params).daily_returns


def summarize(daily_returns: list[float], target_pct: float | None = None) -> dict:
    """Summarize the daily-return distribution.

    ``target_pct`` is optional on purpose: the first question is what the risk
    architecture *can* produce, not whether it clears a number picked in
    advance. Pass one only to test a goal you already have.
    """
    arr = np.asarray(daily_returns, dtype=float)
    n = arr.size

    def pct(p: float) -> float:
        # Linear-interpolated percentile; the previous int(p/100*n) index was
        # a nearest-rank approximation that drifted on small samples.
        return float(np.percentile(arr, p))

    out = {
        "n_days_simulated": n,
        "mean_daily_return_pct": round(float(arr.mean()), 3),
        "median_daily_return_pct": round(float(np.median(arr)), 3),
        "p5_daily_return_pct": round(pct(5), 3),
        "p25_daily_return_pct": round(pct(25), 3),
        "p75_daily_return_pct": round(pct(75), 3),
        "p95_daily_return_pct": round(pct(95), 3),
        "worst_day_pct": round(float(arr.min()), 3),
        "best_day_pct": round(float(arr.max()), 3),
        "pct_days_negative": round(float((arr < 0).mean() * 100), 2),
        "pct_days_flat": round(float((arr == 0).mean() * 100), 2),
    }
    if target_pct is not None:
        hits = int((arr >= target_pct).sum())
        out[f"pct_days_hitting_{target_pct:g}pct_target"] = round(hits / n * 100, 4)
    return out


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
WIDTH = 76


def _supports_unicode() -> bool:
    """Windows consoles still ship codepages that cannot encode box characters.
    Rather than emit mojibake, detect and fall back to ASCII."""
    encoding = getattr(sys.stdout, "encoding", None) or ""
    try:
        "─│╭╰█".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class Glyphs:
    def __init__(self, unicode_ok: bool) -> None:
        if unicode_ok:
            self.h, self.v = "─", "│"
            self.tl, self.tr, self.bl, self.br = "╭", "╮", "╰", "╯"
            self.bar, self.half, self.dot = "█", "▌", "·"
        else:
            self.h, self.v = "-", "|"
            self.tl = self.tr = self.bl = self.br = "+"
            self.bar, self.half, self.dot = "#", "|", "."


# Punctuation that reads well in a UTF-8 terminal but becomes mojibake on a
# legacy Windows codepage. Applied as one pass at the end so report text can
# be written naturally instead of being ASCII-defensive everywhere.
_ASCII_FALLBACKS = str.maketrans({
    "·": "-", "—": "-", "–": "-", "×": "x", "≥": ">=", "≤": "<=",
    "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
})


def _breakeven_win_rate(params: SimParams) -> float:
    """Win rate needed just to break even, costs included.

    Per unit of notional a win returns ``stop_frac * RR`` and a loss costs
    ``stop_frac``, with ``cost_frac`` paid either way. Solving
    ``w*win = (1-w)*loss`` for w gives the floor the strategy must clear
    before it makes a cent — the honest yardstick for any win-rate claim.
    """
    win = params.stop_frac * params.reward_risk - params.cost_frac_of_notional
    loss = params.stop_frac + params.cost_frac_of_notional
    if win <= 0:
        return 100.0  # costs exceed the target: unwinnable at any win rate
    return loss / (win + loss) * 100


def _asciify(text: str) -> str:
    return text.translate(_ASCII_FALLBACKS).encode("ascii", "replace").decode("ascii")


def _box(title: str, g: Glyphs) -> list[str]:
    inner = WIDTH - 2
    return [
        f"{g.tl}{g.h * inner}{g.tr}",
        f"{g.v}{title.center(inner)}{g.v}",
        f"{g.bl}{g.h * inner}{g.br}",
    ]


def _rule(label: str, g: Glyphs) -> str:
    return f"{label} {g.h * max(0, WIDTH - len(label) - 1)}"


def _row(label: str, value: str, width: int = 36) -> str:
    return f"  {label:<{width - 2}}{value}"


def _histogram(returns: np.ndarray, g: Glyphs, bins: int = 21, height: int = 9) -> list[str]:
    """Horizontal distribution histogram, clipped to p1..p99 so a handful of
    extreme days cannot flatten the visible shape."""
    lo, hi = float(np.percentile(returns, 1)), float(np.percentile(returns, 99))
    if hi <= lo:
        return ["  (distribution is degenerate — all days identical)"]

    counts, edges = np.histogram(np.clip(returns, lo, hi), bins=bins, range=(lo, hi))
    peak = counts.max() or 1
    total = counts.sum() or 1
    lines: list[str] = []
    for i, count in enumerate(counts):
        centre = (edges[i] + edges[i + 1]) / 2
        # Quarter-cell resolution so a small bin still shows something rather
        # than rounding away to a blank row.
        filled = int(round(count / peak * height * 4))
        bar = g.bar * (filled // 4) + (g.half if filled % 4 >= 2 else "")
        lines.append(f"  {centre:+7.1f}%  {bar:<{height + 1}} {count / total * 100:5.1f}%")
    return lines


def render_report(params: SimParams, result: SimResult, summary: dict,
                  target_pct: float | None = None,
                  unicode_ok: bool | None = None) -> str:
    use_unicode = _supports_unicode() if unicode_ok is None else unicode_ok
    g = Glyphs(use_unicode)
    arr = np.asarray(result.daily_returns)
    out: list[str] = [""]
    out += _box("Maya  ·  Monte Carlo daily-return simulation", g)
    out.append("")

    # -- assumptions -------------------------------------------------------- #
    out.append(_rule("ASSUMPTIONS", g))
    sizing = {
        "fixed_usd": f"risk ${params.risk_usd:,.2f} to the stop",
        "fixed_margin": f"${params.margin_usd:,.2f} margin at {params.leverage}x",
        "fixed_pct": f"{params.risk_pct:.2f}% of equity (legacy)",
    }[params.risk_model]
    notional = params.notional_at(params.start_equity)
    out.append(_row("Win rate", f"{params.win_rate:.0%}"))
    out.append(_row("Reward : risk", f"1 : {params.reward_risk:.0f}"))
    out.append(_row("Sizing rule", sizing))
    out.append(_row("Position notional", f"${notional:,.2f}"))
    out.append(_row("Margin locked", f"${params.initial_margin_usd:,.2f} "
                                     f"({params.initial_margin_usd / params.start_equity * 100:.1f}% of equity)"))
    out.append(_row("Actual risk to stop",
                    f"${notional * params.stop_frac:,.2f} "
                    f"({params.initial_risk_pct:.2f}% of equity)"))
    out.append(_row("Starting equity", f"${params.start_equity:,.2f}"))
    out.append(_row("Avg stop distance", f"{params.avg_stop_distance_pct:.2f}% of price"))
    out.append(_row("Trade opportunities", f"~{params.trades_per_day_mean:.1f}/day "
                                           f"(cap {params.max_trades_per_day}/day)"))
    out.append(_row("Trades actually taken", f"{result.mean_trades_per_day:.2f}/day"))
    out.append(_row("Risk limits enforced",
                    "yes" if params.enforce_risk_limits else "NO (optimistic)"))
    out.append(_row("Equity between weeks",
                    "compounded" if params.compound
                    else f"reset to ${params.start_equity:,.0f} each week"))
    out.append(_row("Sample", f"{summary['n_days_simulated']:,} days "
                              f"({params.simulations:,} runs x {params.days} days)"))
    out.append("")

    # -- leverage & costs --------------------------------------------------- #
    out.append(_rule("LEVERAGE, LIQUIDATION & COST DRAG", g))
    out.append(_row("Leverage", f"{params.leverage}x"))
    if params.leverage > RiskLimits.MAX_LEVERAGE:
        out.append(_row("  !! exceeds hard limit",
                        f"RiskLimits.MAX_LEVERAGE = {RiskLimits.MAX_LEVERAGE}x"))
    liq_pct = params.liquidation_move_frac * 100
    ratio = params.stop_to_liquidation_ratio
    out.append(_row("Liquidation at", f"{liq_pct:.2f}% adverse move"))
    out.append(_row("Stop at", f"{params.avg_stop_distance_pct:.2f}% adverse move"))
    if ratio >= 1.0:
        verdict = "LIQUIDATED BEFORE THE STOP FILLS"
    elif ratio >= 0.5:
        verdict = f"stop uses {ratio:.0%} of the buffer — dangerously close"
    elif ratio >= 0.25:
        verdict = f"stop uses {ratio:.0%} of the buffer — thin"
    else:
        verdict = f"stop uses {ratio:.0%} of the buffer — comfortable"
    out.append(_row("Safety buffer", verdict))
    out.append(_row("Round-trip cost", f"{params.cost_frac_of_notional * 100:.3f}% of notional"))
    out.append(_row("  = share of every risk unit", f"{params.cost_pct_of_risk:.1f}%"))
    out.append(_row("  breakeven win rate",
                    f"{_breakeven_win_rate(params):.1f}% (vs {params.win_rate:.0%} assumed)"))
    out.append("")

    # -- distribution ------------------------------------------------------- #
    out.append(_rule("DAILY RETURN DISTRIBUTION", g))
    out += _histogram(arr, g)
    out.append("")

    out.append(_rule("PERCENTILES", g))
    out.append(_row("Worst day", f"{summary['worst_day_pct']:+.2f}%"))
    out.append(_row("5th percentile", f"{summary['p5_daily_return_pct']:+.2f}%"))
    out.append(_row("25th percentile", f"{summary['p25_daily_return_pct']:+.2f}%"))
    out.append(_row("Median", f"{summary['median_daily_return_pct']:+.2f}%"))
    out.append(_row("Mean", f"{summary['mean_daily_return_pct']:+.2f}%"))
    out.append(_row("75th percentile", f"{summary['p75_daily_return_pct']:+.2f}%"))
    out.append(_row("95th percentile", f"{summary['p95_daily_return_pct']:+.2f}%"))
    out.append(_row("Best day", f"{summary['best_day_pct']:+.2f}%"))
    out.append(_row("Losing days", f"{summary['pct_days_negative']:.1f}%"))
    # On a selective 1m strategy most days simply have no qualifying setup, and
    # a median of 0.00% is a flat day rather than a breakeven trade — worth
    # naming so the median is not misread.
    out.append(_row("Flat days (no trade)", f"{summary['pct_days_flat']:.1f}%"))
    out.append("")

    # -- risk architecture -------------------------------------------------- #
    out.append(_rule("HOW OFTEN THE RISK LIMITS FIRED", g))
    if params.enforce_risk_limits:
        out.append(_row("Days cut by 3% daily loss", f"{result.pct_days_halted_daily_loss:.2f}%"))
        out.append(_row("Days ended by cooldown", f"{result.pct_days_ended_by_cooldown:.2f}%"))
        out.append(_row("Weeks cut by 8% kill switch", f"{result.pct_weeks_halted_weekly_loss:.2f}%"))
        out.append(_row("Runs hitting the equity floor", f"{result.pct_sims_hitting_equity_floor:.2f}%"))
    else:
        out.append("  (disabled — these limits were not simulated)")
    out.append("")

    # -- verdict ------------------------------------------------------------ #
    if target_pct is not None:
        hit_rate = summary[f"pct_days_hitting_{target_pct:g}pct_target"]
        out.append(_rule(f"VERDICT vs {target_pct:g}% DAILY TARGET", g))
        if hit_rate < 0.01:
            verdict = "ESSENTIALLY UNREACHABLE"
            detail = "fewer than 1 day in 10,000 clears the target"
        elif hit_rate < 1:
            verdict = "RARE"
            detail = f"roughly 1 day in {round(100 / max(hit_rate, 1e-9)):,} clears it"
        elif hit_rate < 10:
            verdict = "OCCASIONAL"
            detail = f"about 1 day in {round(100 / hit_rate)} clears it"
        else:
            verdict = "REGULARLY REACHED"
            detail = f"{hit_rate:.1f}% of days clear it"
        out.append(_row("Days hitting target", f"{hit_rate:.4f}%"))
        out.append(_row("Read", f"{verdict} — {detail}"))
        out.append("")

    # -- caveats ------------------------------------------------------------ #
    out.append(_rule("READ THIS BEFORE BELIEVING IT", g))
    for line in [
        "Outcomes are i.i.d. per trade; real markets cluster into regimes, so",
        "true variance is very likely higher than shown. Wins take the full",
        "target and losses the full stop — no partials, breakeven stop-outs or",
        "max-duration exits. The 60-minute cooldown is modeled as ending the",
        "trading day. This is a model of the risk architecture, not evidence",
        "of an edge: it assumes the win rate you gave it.",
    ]:
        out.append(f"  {g.dot} {line}")
    out.append("")
    report = "\n".join(out)
    return report if use_unicode else _asciify(report)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m maya.backtest.monte_carlo",
        description="Monte Carlo daily-return simulation using Maya's real risk limits.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    d = SimParams()
    p.add_argument("--win-rate", type=float, default=float(os.environ.get("MC_WIN_RATE", d.win_rate)),
                   help="probability a trade reaches its target")
    p.add_argument("--trades-per-day", type=float,
                   default=float(os.environ.get("MC_TRADES_PER_DAY", d.trades_per_day_mean)),
                   help="mean trade opportunities per day")
    p.add_argument("--stop-dist", type=float,
                   default=float(os.environ.get("MC_STOP_DIST_PCT", d.avg_stop_distance_pct)),
                   help="average stop distance, %% of price")
    p.add_argument("--target", type=float,
                   default=(float(os.environ["MC_TARGET_PCT"])
                            if os.environ.get("MC_TARGET_PCT") else None),
                   help="optional DAILY return target to evaluate, %%. Omit to just "
                        "report the achievable distribution")
    p.add_argument("--simulations", type=int,
                   default=int(os.environ.get("MC_SIMULATIONS", d.simulations)),
                   help="independent runs")
    p.add_argument("--days", type=int, default=d.days, help="trading days per run")
    p.add_argument("--risk-usd", type=float, default=d.risk_usd, help="USD risked to the stop (fixed_usd)")
    p.add_argument("--margin-usd", type=float, default=d.margin_usd,
                   help="USD of margin committed per trade (fixed_margin)")
    p.add_argument("--start-equity", type=float, default=d.start_equity, help="starting equity, USD")
    p.add_argument("--risk-model", choices=("fixed_usd", "fixed_margin", "fixed_pct"),
                   default=d.risk_model,
                   help="fixed_usd matches the live risk manager; fixed_margin is "
                        "'$X at Nx leverage'; fixed_pct is legacy")
    p.add_argument("--leverage", type=int, default=d.leverage, help="leverage multiplier")
    p.add_argument("--slippage-bps", type=float, default=d.slippage_bps,
                   help="slippage per side, bps of notional")
    p.add_argument("--funding-rate", type=float, default=d.funding_rate_hourly,
                   help="perp funding rate per HOUR, as a fraction "
                        "(Hyperliquid charges hourly, not 8-hourly)")
    p.add_argument("--fee-rate", type=float, default=d.fee_rate,
                   help="per-side fee rate as a fraction")
    p.add_argument("--hold-hours", type=float, default=d.avg_hold_hours,
                   help="average position hold time, hours (drives funding cost)")
    p.add_argument("--seed", type=int, default=d.seed)
    p.add_argument("--no-risk-limits", action="store_true",
                   help="disable the daily/weekly halts and cooldown (optimistic)")
    p.add_argument("--compound", action="store_true",
                   help="carry equity week to week (answers 'what might a year do' "
                        "instead of 'what does a week look like from here')")
    p.add_argument("--json", action="store_true", help="emit JSON instead of the report")
    p.add_argument("--ascii", action="store_true", help="force ASCII output")
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    params = SimParams(
        win_rate=args.win_rate,
        trades_per_day_mean=args.trades_per_day,
        avg_stop_distance_pct=args.stop_dist,
        simulations=args.simulations,
        days=args.days,
        risk_usd=args.risk_usd,
        margin_usd=args.margin_usd,
        start_equity=args.start_equity,
        risk_model=args.risk_model,
        leverage=args.leverage,
        slippage_bps=args.slippage_bps,
        funding_rate_hourly=args.funding_rate,
        fee_rate=args.fee_rate,
        avg_hold_hours=args.hold_hours,
        seed=args.seed,
        enforce_risk_limits=not args.no_risk_limits,
        compound=args.compound,
    )
    # Status goes to stderr so `--json` and `> file` stay clean.
    total_days = params.simulations * params.days
    print(f"Simulating {total_days:,} days "
          f"({params.simulations:,} runs x {params.days} days)...",
          file=sys.stderr, flush=True)

    started = time.perf_counter()
    result = simulate_detailed(params)
    elapsed = time.perf_counter() - started
    summary = summarize(result.daily_returns, target_pct=args.target)
    print(f"Done in {elapsed:.1f}s.", file=sys.stderr, flush=True)

    if args.json:
        print(json.dumps({
            "params": asdict(params),
            "summary": summary,
            "risk_limit_activity": {
                "pct_days_halted_daily_loss": round(result.pct_days_halted_daily_loss, 4),
                "pct_days_ended_by_cooldown": round(result.pct_days_ended_by_cooldown, 4),
                "pct_weeks_halted_weekly_loss": round(result.pct_weeks_halted_weekly_loss, 4),
                "pct_sims_hitting_equity_floor": round(result.pct_sims_hitting_equity_floor, 4),
                "mean_trades_per_day": round(result.mean_trades_per_day, 3),
            },
        }, indent=2))
        return

    print(render_report(params, result, summary, args.target,
                        unicode_ok=False if args.ascii else None))


if __name__ == "__main__":
    main()
