"""Retry delay: exponential backoff with full jitter."""

from __future__ import annotations

import random


def backoff_delay(attempt: int, base_s: float, cap_s: float, rng: random.Random | None = None) -> float:
    """Seconds to wait after ``attempt`` attempts have failed.

    The ceiling doubles each time (base, 2*base, 4*base, ...) up to ``cap_s``. "Full jitter" then
    picks a random delay between 0 and the ceiling, so thousands of files that failed together do
    not retry together: that is how a retry storm (F08, F21) is avoided.
    """
    ceiling = min(cap_s, base_s * (2 ** max(attempt - 1, 0)))
    return (rng or random).uniform(0, ceiling)