"""Flow — what aggressive traders are actually doing.

Reads delta (real taker buy/sell split), absorption and the volume profile.

**One deliberate inversion.** The setups in ``maya.strategy.setups`` treat
positive delta as bullish confirmation. Measured over 24 months of real order
flow on 15m bars, positive delta preceded *negative* forward returns in 8 of 8
sub-periods on BTC and SOL — short-term mean reversion, the market-maker
inventory effect. The full-pipeline factor decomposition agreed independently:
trades where the ``cvd`` factor fired won 2.8 points *less* often.

So Flow reads delta as mildly contrarian, per ``DELTA_IS_MOMENTUM = False``.
This does not change the legacy setups — Flow is a separate analysis layer, and
silently flipping a live strategy on the back of a sub-2-bps effect would be
reckless. It encodes what was measured, and leaves the old path alone until
someone deliberately switches it.
"""
from __future__ import annotations

from maya.agents.models import BEARISH, BULLISH, NEUTRAL, Finding
from maya.signals.models import SignalObject

AGENT = "Flow"

# See module docstring. False = delta is read as mean-reverting, which is what
# the data showed. Flip to True to restore the original momentum reading.
DELTA_IS_MOMENTUM = False
# Delta must be this share of the bar's volume before it says anything; small
# imbalances are noise on any timeframe.
MIN_DELTA_SHARE = 0.15
# The measured effect is ~1 bps against ~10 bps of cost, so Flow is capped low
# on delta alone. Absorption, which is OHLCV-derived and independent, can say
# more.
DELTA_MAX_CONFIDENCE = 0.30
ABSORPTION_MAX_CONFIDENCE = 0.55


def observe(sig: SignalObject) -> Finding:
    """Flow's read of the current bar's order flow."""
    data = {
        "delta": sig.cvd_1m,
        "cvd_session": sig.cvd_session,
        "divergence": sig.cvd_divergence,
        "absorption": sig.absorption.side if sig.absorption.detected else None,
        "amt_phase": sig.amt_phase,
    }

    # Absorption first: repeated heavy wicks into a level is a stronger and
    # better-behaved statement than a single bar's delta.
    ab = sig.absorption
    if ab.detected and ab.side:
        verdict = BULLISH if ab.side == "bullish" else BEARISH
        conf = round(min(ab.strength, 1.0) * ABSORPTION_MAX_CONFIDENCE, 4)
        level = f" at {ab.level:.2f}" if ab.level is not None else ""
        return Finding(AGENT, verdict, conf,
                       f"{ab.side} absorption{level} "
                       f"(strength {ab.strength:.2f})", data)

    if not sig.volume or sig.cvd_1m == 0:
        return Finding(AGENT, NEUTRAL, 0.0, "no meaningful order-flow signal", data)

    share = abs(sig.cvd_1m) / sig.volume
    if share < MIN_DELTA_SHARE:
        return Finding(AGENT, NEUTRAL, 0.0,
                       f"delta only {share:.0%} of volume — noise", data)

    buyers_dominant = sig.cvd_1m > 0
    if DELTA_IS_MOMENTUM:
        verdict = BULLISH if buyers_dominant else BEARISH
        reading = "momentum"
    else:
        verdict = BEARISH if buyers_dominant else BULLISH
        reading = "mean-reverting (measured)"

    conf = round(min(share, 1.0) * DELTA_MAX_CONFIDENCE, 4)
    side = "buyers" if buyers_dominant else "sellers"
    return Finding(AGENT, verdict, conf,
                   f"{side} aggressive, delta {share:.0%} of volume — "
                   f"read as {reading}",
                   data | {"delta_share": round(share, 4)})
