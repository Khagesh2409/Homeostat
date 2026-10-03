"""
Fallback and circuit breaker management for Bedrock LLM calls.

Provides graceful degradation when Bedrock is unavailable or throttling.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreaker:
    """Circuit breaker for Bedrock invocations."""

    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self._consecutive_failures = 0
        self._tripped_at: float | None = None

    @property
    def is_available(self) -> bool:
        """True if the circuit is CLOSED (normal) or half-open after cooldown."""
        if self._tripped_at is None:
            return True

        elapsed = self.clock() - self._tripped_at
        if elapsed >= self.cooldown_seconds:
            # Half-open: allow probe request
            return True

        return False

    def record_success(self) -> None:
        """Reset failures on successful request."""
        self._consecutive_failures = 0
        self._tripped_at = None

    def record_failure(self, error: Exception | str) -> None:
        """Record a failure; trip breaker if threshold reached."""
        self._consecutive_failures += 1
        now = self.clock()
        logger.warning(
            "Bedrock failure recorded (%d/%d): %s",
            self._consecutive_failures,
            self.failure_threshold,
            error,
        )

        if self._consecutive_failures >= self.failure_threshold:
            self._tripped_at = now
            logger.error(
                "Bedrock circuit breaker TRIPPED. Degraded mode active for %.0fs. "
                "Falling back to Tier-0 only.",
                self.cooldown_seconds,
            )

    def reset(self) -> None:
        """Reset circuit breaker state."""
        self._consecutive_failures = 0
        self._tripped_at = None


default_circuit_breaker = CircuitBreaker()


def is_bedrock_available() -> bool:
    """Global check for Bedrock availability."""
    return default_circuit_breaker.is_available
