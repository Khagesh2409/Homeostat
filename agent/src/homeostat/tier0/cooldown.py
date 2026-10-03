"""
Cooldown and miss tracking for Tier-0 playbooks.

Two safety rails live here:

1. Cooldown: the same playbook must not hit the same target twice within
   ``cooldown_seconds``, and must not exceed ``max_attempts`` within a rolling
   ``window_seconds``. This stops the classic "restart the pod 10 times a
   minute" loop.

2. Miss tracking: if a Tier-0 action ran but verification failed, that is a
   "miss". Once a target has a miss inside the window, Tier-0 stands down for
   that target and the incident goes straight to the LLM path.

State is in-memory and process-local. That is deliberate: if the agent
restarts, the worst case is one extra deterministic attempt, which is cheap.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CooldownDecision:
    """Whether a Tier-0 action is allowed right now, and why not if it isn't."""

    allowed: bool
    reason: str = ""


class CooldownTracker:
    """Thread-safe record of Tier-0 attempts and misses, keyed by target."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._misses: dict[str, deque[float]] = defaultdict(deque)

    # ── Queries ───────────────────────────────────────────────────────────

    def check(
        self,
        key: str,
        *,
        cooldown_seconds: int,
        max_attempts: int,
        window_seconds: int,
    ) -> CooldownDecision:
        """Decide whether a new attempt on ``key`` is allowed."""
        now = self._clock()
        with self._lock:
            attempts = self._prune(self._attempts, key, now, window_seconds)
            misses = self._prune(self._misses, key, now, window_seconds)

            if misses:
                return CooldownDecision(
                    False, f"Tier-0 already missed on {key} within {window_seconds}s"
                )

            if attempts:
                elapsed = now - attempts[-1]
                if elapsed < cooldown_seconds:
                    remaining = int(cooldown_seconds - elapsed)
                    return CooldownDecision(
                        False, f"{key} is cooling down ({remaining}s remaining)"
                    )

            if len(attempts) >= max_attempts:
                return CooldownDecision(
                    False,
                    f"{key} hit max attempts ({max_attempts}) within {window_seconds}s",
                )

        return CooldownDecision(True)

    def attempts(self, key: str) -> int:
        """Total recorded attempts for ``key`` (unpruned, for observability)."""
        with self._lock:
            return len(self._attempts.get(key, ()))

    def misses(self, key: str) -> int:
        """Total recorded misses for ``key`` (unpruned, for observability)."""
        with self._lock:
            return len(self._misses.get(key, ()))

    # ── Mutations ─────────────────────────────────────────────────────────

    def record_attempt(self, key: str) -> None:
        """Mark that a Tier-0 playbook is acting on ``key`` now."""
        with self._lock:
            self._attempts[key].append(self._clock())

    def record_miss(self, key: str) -> None:
        """Mark that a Tier-0 action on ``key`` failed verification."""
        with self._lock:
            self._misses[key].append(self._clock())

    def reset(self) -> None:
        """Forget everything. Intended for tests."""
        with self._lock:
            self._attempts.clear()
            self._misses.clear()

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _prune(
        store: dict[str, deque[float]], key: str, now: float, window_seconds: int
    ) -> deque[float]:
        """Drop timestamps older than the window and return what's left."""
        entries = store.get(key)
        if entries is None:
            return deque()
        while entries and now - entries[0] > window_seconds:
            entries.popleft()
        if not entries:
            del store[key]
            return deque()
        return entries


# Process-wide tracker used by the graph nodes.
default_tracker = CooldownTracker()
