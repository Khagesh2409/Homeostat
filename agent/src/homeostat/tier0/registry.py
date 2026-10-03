"""
Tier-0 registry: maps an alert to at most one playbook, with cooldown applied.

Two entry points:

  is_tier0_candidate(alert)  -> cheap alertname check used by triage
  resolve_playbook(...)      -> full match + cooldown check used by the tier0 node
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from homeostat.preprocessor.schemas import ErrorSignature
from homeostat.state import ActionStep, Alert
from homeostat.tier0.cooldown import CooldownTracker, default_tracker
from homeostat.tier0.playbooks import PLAYBOOKS, Tier0Playbook

logger = logging.getLogger(__name__)

# Every alertname any playbook could handle. Triage uses this as a fast gate.
TIER0_ALERTNAMES: frozenset[str] = frozenset().union(*(p.alertnames for p in PLAYBOOKS))


@dataclass(frozen=True)
class Tier0Match:
    """A playbook bound to a concrete alert, ready to become an ActionPlan."""

    playbook: Tier0Playbook
    target: str
    actions: list[ActionStep] = field(default_factory=list)
    verify_steps: list[ActionStep] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.playbook.name


@dataclass(frozen=True)
class Tier0Resolution:
    """Outcome of resolving an alert. ``match`` is None when Tier-0 should stand down."""

    match: Tier0Match | None
    reason: str


def is_tier0_candidate(alert: Alert) -> bool:
    """True if at least one playbook might handle this alert."""
    return alert.alertname in TIER0_ALERTNAMES


def find_playbook(
    alert: Alert,
    signatures: Sequence[ErrorSignature] = (),
    playbooks: Sequence[Tier0Playbook] = PLAYBOOKS,
) -> Tier0Playbook | None:
    """Return the first playbook whose predicate matches. Pure, no cooldown."""
    for playbook in playbooks:
        if playbook.matches(alert, signatures):
            return playbook
    return None


def resolve_playbook(
    alert: Alert,
    signatures: Sequence[ErrorSignature] = (),
    *,
    tracker: CooldownTracker | None = None,
    playbooks: Sequence[Tier0Playbook] = PLAYBOOKS,
) -> Tier0Resolution:
    """
    Find a playbook for ``alert`` and check it is allowed to run right now.

    Does not record the attempt. The caller records it once it commits to
    acting, so a resolution that is never executed doesn't burn a retry.
    """
    tracker = tracker or default_tracker

    playbook = find_playbook(alert, signatures, playbooks)
    if playbook is None:
        return Tier0Resolution(None, f"No playbook matched {alert.alertname} on {alert.source}")

    target = playbook.target(alert)
    decision = tracker.check(
        cooldown_key(playbook.name, target),
        cooldown_seconds=playbook.cooldown_seconds,
        max_attempts=playbook.max_retries,
        window_seconds=playbook.window_seconds,
    )
    if not decision.allowed:
        logger.info("Tier-0 %s blocked: %s", playbook.name, decision.reason)
        return Tier0Resolution(None, f"Playbook '{playbook.name}' blocked: {decision.reason}")

    return Tier0Resolution(
        Tier0Match(
            playbook=playbook,
            target=target,
            actions=playbook.actions(alert),
            verify_steps=playbook.verify(alert),
        ),
        f"Matched playbook '{playbook.name}' for {target}",
    )


def cooldown_key(playbook_name: str, target: str) -> str:
    """The tracker key used for a playbook acting on a target."""
    return f"{playbook_name}:{target}"
