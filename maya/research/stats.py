"""Statistics for the research harness, stdlib only.

The t-stat uses the normal approximation for its threshold. That is fine
here because the pass rule already demands at least 100 trades, where the
t and normal distributions differ in the second decimal place.
"""
from __future__ import annotations

import math
from statistics import NormalDist

# The old bar every README measurement used. The corrected threshold never
# drops below it, so the first registered test is judged no more kindly than
# the nine conditions already measured.
BASE_T_THRESHOLD = 2.0


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def t_stat(xs: list[float]) -> float:
    """One-sample t-stat of the mean against zero. 0.0 when undefined."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var / n)


def corrected_threshold(n_tests: int, alpha: float = 0.05) -> float:
    """Absolute t a result must clear after ``n_tests`` tests (Bonferroni).

    Two-sided: each test gets ``alpha / n_tests``, split across both tails.
    Every test ever registered counts, failures included, because a failure
    you quietly forget is how fifty tries turn into one "discovery".
    """
    if n_tests < 1:
        raise ValueError("n_tests must be at least 1")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    z = NormalDist().inv_cdf(1.0 - alpha / (2.0 * n_tests))
    return max(BASE_T_THRESHOLD, z)
