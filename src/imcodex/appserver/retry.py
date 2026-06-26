from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass


RandomFloat = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RetryBackoff:
    max_attempts: int = 3
    initial_delay_s: float = 0.25
    max_delay_s: float = 2.0
    jitter_fraction: float = 0.25

    @property
    def attempts(self) -> int:
        return max(1, int(self.max_attempts))

    def with_max_attempts(self, max_attempts: int) -> "RetryBackoff":
        return RetryBackoff(
            max_attempts=max_attempts,
            initial_delay_s=self.initial_delay_s,
            max_delay_s=self.max_delay_s,
            jitter_fraction=self.jitter_fraction,
        )

    def delay_after_failure(self, failed_attempt: int, *, random_float: RandomFloat | None = None) -> float:
        if failed_attempt < 1:
            return 0.0
        initial = max(0.0, float(self.initial_delay_s))
        maximum = max(initial, float(self.max_delay_s))
        if initial == 0.0 or maximum == 0.0:
            return 0.0
        base = min(maximum, initial * (2 ** (failed_attempt - 1)))
        jitter_fraction = max(0.0, float(self.jitter_fraction))
        jitter_source = random_float or random.random
        jitter = base * jitter_fraction * max(0.0, min(1.0, float(jitter_source())))
        return min(maximum, base + jitter)
