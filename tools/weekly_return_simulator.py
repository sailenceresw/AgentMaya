"""Monte Carlo simulator: what does a realistic week actually look like under
Maya's real risk parameters, and how often does a 40%/week floor get hit?

Uses the system's actual constants (maya/config.py, maya/execution/engine.py):
  - risk per trade  = 0.5% of equity   (MAX_RISK_PER_TRADE_PCT)
  - reward per win  = risk * 4         (MIN_RR_FOR_ENTRY, typical setups
                                         target near the minimum, not far past it)
  - fees            = 0.05% taker, round trip on ~1x-equity notional
                       (representative for a ~0.5% stop distance)
  - trades/week     = capped at 25 (5/day x 5 days), but real selectivity
                       means far fewer setups actually clear the confluence bar

Three win-rate scenarios spanning the system's own documented realistic
range (43-49%), and three trade-frequency scenarios (how many A+ setups
actually show up in a week) since that's genuinely unknown until this runs
on real testnet data.
"""
import random
import statistics

RISK_PCT = 0.005      # 0.5% of equity risked per trade
RR = 4.0               # reward:risk
WIN_PCT = RISK_PCT * RR   # 2% equity gain on a winning trade
LOSS_PCT = RISK_PCT       # 0.5% equity loss on a losing trade
FEE_PCT_OF_EQUITY = 0.001  # ~0.05% taker x2 (entry+exit) on ~1x-equity notional

WIN_RATE_SCENARIOS = {"conservative (43%)": 0.43, "base (46%)": 0.46, "optimistic (49%)": 0.49}
TRADES_PER_WEEK_SCENARIOS = {"low selectivity (5/wk avg)": 5,
                              "medium selectivity (12/wk avg)": 12,
                              "max allowed (25/wk cap)": 25}

N_SIMS = 20000
random.seed(42)


def simulate_week(win_rate: float, avg_trades: int) -> float:
    n_trades = min(25, max(0, round(random.gauss(avg_trades, avg_trades * 0.3))))
    equity = 1.0
    for _ in range(n_trades):
        equity *= (1 - FEE_PCT_OF_EQUITY)
        if random.random() < win_rate:
            equity *= (1 + WIN_PCT)
        else:
            equity *= (1 - LOSS_PCT)
    return (equity - 1.0) * 100  # % return for the week


print(f"{'win rate':<20}{'trades/wk':<24}{'median':>9}{'mean':>9}{'p10':>9}{'p90':>9}{'P(>=40%)':>11}")
print("-" * 92)
results = {}
for wr_label, wr in WIN_RATE_SCENARIOS.items():
    for tf_label, avg_t in TRADES_PER_WEEK_SCENARIOS.items():
        weekly_returns = [simulate_week(wr, avg_t) for _ in range(N_SIMS)]
        weekly_returns.sort()
        median = statistics.median(weekly_returns)
        mean = statistics.mean(weekly_returns)
        p10 = weekly_returns[int(0.10 * N_SIMS)]
        p90 = weekly_returns[int(0.90 * N_SIMS)]
        hit_40 = sum(1 for r in weekly_returns if r >= 40) / N_SIMS * 100
        results[(wr_label, tf_label)] = weekly_returns
        print(f"{wr_label:<20}{tf_label:<24}{median:>8.2f}%{mean:>8.2f}%{p10:>8.2f}%{p90:>8.2f}%{hit_40:>10.3f}%")

import json
with open("/home/claude/AgentMaya/tools/sim_results.json", "w") as f:
    json.dump({f"{k[0]}|{k[1]}": v for k, v in results.items()}, f)
