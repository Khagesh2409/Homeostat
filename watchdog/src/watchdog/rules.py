"""Deterministic safety rules enforced by the Homeostat watchdog."""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, ClassVar

import structlog

logger = structlog.get_logger(__name__)


class RuleSeverity(str, Enum):
    """Severity classification for rule evaluations."""

    INFO = "INFO"
    WARNING = "WARNING"
    VIOLATION = "VIOLATION"


@dataclass
class RuleEvaluation:
    """Outcome of a single rule evaluation."""

    rule_name: str
    passed: bool
    severity: RuleSeverity
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SpendCapRule:
    """Enforces monthly AWS spend cap on the agent infrastructure."""

    def __init__(
        self,
        spend_cap_usd: float = 20.0,
        warning_threshold_usd: float = 16.0,
    ) -> None:
        self.spend_cap_usd = spend_cap_usd
        self.warning_threshold_usd = warning_threshold_usd

    def evaluate(self, current_spend_usd: float) -> RuleEvaluation:
        """Evaluate current spend against spend cap and warning thresholds."""
        if current_spend_usd >= self.spend_cap_usd:
            return RuleEvaluation(
                rule_name="SpendCapRule",
                passed=False,
                severity=RuleSeverity.VIOLATION,
                message=(
                    f"Monthly spend cap exceeded: ${current_spend_usd:.2f} "
                    f"exceeds limit of ${self.spend_cap_usd:.2f}"
                ),
                details={
                    "current_spend_usd": current_spend_usd,
                    "spend_cap_usd": self.spend_cap_usd,
                },
            )

        if current_spend_usd >= self.warning_threshold_usd:
            return RuleEvaluation(
                rule_name="SpendCapRule",
                passed=True,
                severity=RuleSeverity.WARNING,
                message=(
                    f"Monthly spend approaching cap: ${current_spend_usd:.2f} "
                    f"(warning at ${self.warning_threshold_usd:.2f}, "
                    f"limit ${self.spend_cap_usd:.2f})"
                ),
                details={
                    "current_spend_usd": current_spend_usd,
                    "warning_threshold_usd": self.warning_threshold_usd,
                    "spend_cap_usd": self.spend_cap_usd,
                },
            )

        return RuleEvaluation(
            rule_name="SpendCapRule",
            passed=True,
            severity=RuleSeverity.INFO,
            message=f"Spend ${current_spend_usd:.2f} within limit ${self.spend_cap_usd:.2f}",
            details={"current_spend_usd": current_spend_usd},
        )


class RateLimitRule:
    """Enforces maximum incident actions per hour using a sliding time window."""

    def __init__(self, max_incidents_per_hour: int = 10) -> None:
        self.max_incidents_per_hour = max_incidents_per_hour
        self._action_timestamps: deque[datetime] = deque()

    def record_action(
        self,
        incident_id: str,
        timestamp: datetime | None = None,
    ) -> None:
        """Record an incident action timestamp."""
        ts = timestamp or datetime.now(UTC)
        self._action_timestamps.append(ts)
        logger.info(
            "watchdog_rate_limit_action_recorded",
            incident_id=incident_id,
            timestamp=ts.isoformat(),
        )

    def _prune(self, current_time: datetime) -> None:
        """Remove actions older than 1 hour from the sliding window."""
        cutoff = current_time - timedelta(hours=1)
        while self._action_timestamps and self._action_timestamps[0] < cutoff:
            self._action_timestamps.popleft()

    def get_action_count(self, current_time: datetime | None = None) -> int:
        """Return the number of actions in the last hour."""
        now = current_time or datetime.now(UTC)
        self._prune(now)
        return len(self._action_timestamps)

    def evaluate(self, current_time: datetime | None = None) -> RuleEvaluation:
        """Evaluate whether the agent has exceeded its hourly action rate limit."""
        now = current_time or datetime.now(UTC)
        count = self.get_action_count(now)

        if count > self.max_incidents_per_hour:
            return RuleEvaluation(
                rule_name="RateLimitRule",
                passed=False,
                severity=RuleSeverity.VIOLATION,
                message=(
                    f"Rate limit exceeded: {count} actions in last hour "
                    f"(maximum {self.max_incidents_per_hour})"
                ),
                details={
                    "actions_last_hour": count,
                    "max_incidents_per_hour": self.max_incidents_per_hour,
                },
            )

        return RuleEvaluation(
            rule_name="RateLimitRule",
            passed=True,
            severity=RuleSeverity.INFO,
            message=f"{count}/{self.max_incidents_per_hour} actions in last hour",
            details={
                "actions_last_hour": count,
                "max_incidents_per_hour": self.max_incidents_per_hour,
            },
        )

    def reset(self) -> None:
        """Clear recorded action history."""
        self._action_timestamps.clear()


class ScopeBoundaryRule:
    """Prevents agent from modifying watchdog resources or its own security boundary."""

    FORBIDDEN_IAM_ROLES: ClassVar[set[str]] = {"homeostat-watchdog", "homeostat-agent"}
    FORBIDDEN_SECURITY_GROUPS: ClassVar[set[str]] = {
        "homeostat-watchdog-sg",
        "watchdog-sg",
    }
    FORBIDDEN_ACTIONS: ClassVar[set[str]] = {
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutUserPolicy",
        "iam:DeleteUserPolicy",
        "iam:DeleteRolePermissionsBoundary",
        "iam:PutRolePermissionsBoundary",
    }

    def evaluate_target(
        self,
        target_resource: str,
        action: str = "",
    ) -> RuleEvaluation:
        """Evaluate if an action or target violates the external scope boundary."""
        target_lower = target_resource.lower()
        action_lower = action.lower()

        # Check IAM and security group targets
        for forbidden_role in self.FORBIDDEN_IAM_ROLES:
            if forbidden_role.lower() in target_lower:
                return RuleEvaluation(
                    rule_name="ScopeBoundaryRule",
                    passed=False,
                    severity=RuleSeverity.VIOLATION,
                    message=(
                        f"Scope boundary violation: Agent cannot modify protected role "
                        f"'{forbidden_role}'"
                    ),
                    details={"target_resource": target_resource, "action": action},
                )

        for forbidden_sg in self.FORBIDDEN_SECURITY_GROUPS:
            if forbidden_sg.lower() in target_lower:
                return RuleEvaluation(
                    rule_name="ScopeBoundaryRule",
                    passed=False,
                    severity=RuleSeverity.VIOLATION,
                    message=(
                        f"Scope boundary violation: Agent cannot modify watchdog security group "
                        f"'{forbidden_sg}'"
                    ),
                    details={"target_resource": target_resource, "action": action},
                )

        # Check forbidden IAM actions
        for forbidden_action in self.FORBIDDEN_ACTIONS:
            if forbidden_action.lower() == action_lower:
                return RuleEvaluation(
                    rule_name="ScopeBoundaryRule",
                    passed=False,
                    severity=RuleSeverity.VIOLATION,
                    message=(
                        f"Scope boundary violation: Agent is forbidden from executing "
                        f"'{forbidden_action}'"
                    ),
                    details={"target_resource": target_resource, "action": action},
                )

        return RuleEvaluation(
            rule_name="ScopeBoundaryRule",
            passed=True,
            severity=RuleSeverity.INFO,
            message="Target resource and action conform to scope boundaries",
            details={"target_resource": target_resource, "action": action},
        )


class BlastRadiusRule:
    """Enforces blast radius constraints: pod deletion caps and namespace protection."""

    def __init__(self, max_pod_deletions_per_incident: int = 5) -> None:
        self.max_pod_deletions_per_incident = max_pod_deletions_per_incident
        self._incident_deletions: dict[str, int] = defaultdict(int)

    def evaluate_namespace_operation(
        self,
        action: str,
        namespace: str,
    ) -> RuleEvaluation:
        """Reject any attempt to delete or destroy a namespace."""
        action_lower = action.lower()
        if "delete" in action_lower or "destroy" in action_lower:
            return RuleEvaluation(
                rule_name="BlastRadiusRule",
                passed=False,
                severity=RuleSeverity.VIOLATION,
                message=(
                    f"Blast radius violation: Namespace deletion is strictly forbidden "
                    f"(target: '{namespace}')"
                ),
                details={"action": action, "namespace": namespace},
            )

        return RuleEvaluation(
            rule_name="BlastRadiusRule",
            passed=True,
            severity=RuleSeverity.INFO,
            message=f"Namespace operation '{action}' on '{namespace}' permitted",
            details={"action": action, "namespace": namespace},
        )

    def record_pod_deletions(self, incident_id: str, count: int = 1) -> None:
        """Record deleted pod count for an incident."""
        self._incident_deletions[incident_id] += count

    def evaluate_pod_deletion(
        self,
        incident_id: str,
        count: int = 1,
    ) -> RuleEvaluation:
        """Evaluate if proposed pod deletion exceeds blast radius limit for this incident."""
        current = self._incident_deletions[incident_id]
        total_after = current + count

        if total_after > self.max_pod_deletions_per_incident:
            return RuleEvaluation(
                rule_name="BlastRadiusRule",
                passed=False,
                severity=RuleSeverity.VIOLATION,
                message=(
                    f"Blast radius violation: Exceeds max pod deletions for incident "
                    f"'{incident_id}' ({total_after} requested, max "
                    f"{self.max_pod_deletions_per_incident})"
                ),
                details={
                    "incident_id": incident_id,
                    "already_deleted": current,
                    "requested": count,
                    "max_allowed": self.max_pod_deletions_per_incident,
                },
            )

        return RuleEvaluation(
            rule_name="BlastRadiusRule",
            passed=True,
            severity=RuleSeverity.INFO,
            message=(
                f"Pod deletion approved: {total_after}/{self.max_pod_deletions_per_incident} "
                f"for incident '{incident_id}'"
            ),
            details={
                "incident_id": incident_id,
                "current_count": total_after,
                "max_allowed": self.max_pod_deletions_per_incident,
            },
        )

    def reset(self) -> None:
        """Reset deletion tracking."""
        self._incident_deletions.clear()


class HeartbeatRule:
    """Verifies that the agent sends regular heartbeats within timeout."""

    def __init__(self, timeout_seconds: int = 300) -> None:
        self.timeout_seconds = timeout_seconds
        self.last_heartbeat_at: datetime | None = None
        self.last_metadata: dict[str, Any] = {}

    def record_heartbeat(
        self,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a heartbeat receipt."""
        self.last_heartbeat_at = timestamp or datetime.now(UTC)
        self.last_metadata = metadata or {}
        logger.debug(
            "watchdog_heartbeat_received",
            timestamp=self.last_heartbeat_at.isoformat(),
        )

    def evaluate(self, current_time: datetime | None = None) -> RuleEvaluation:
        """Check if the agent heartbeat has timed out."""
        now = current_time or datetime.now(UTC)

        if self.last_heartbeat_at is None:
            return RuleEvaluation(
                rule_name="HeartbeatRule",
                passed=True,
                severity=RuleSeverity.INFO,
                message="Heartbeat monitoring initialized; waiting for first agent ping",
            )

        elapsed = (now - self.last_heartbeat_at).total_seconds()
        if elapsed > self.timeout_seconds:
            return RuleEvaluation(
                rule_name="HeartbeatRule",
                passed=False,
                severity=RuleSeverity.VIOLATION,
                message=(
                    f"Agent unresponsive: No heartbeat received for {int(elapsed)}s "
                    f"(timeout is {self.timeout_seconds}s)"
                ),
                details={
                    "elapsed_seconds": elapsed,
                    "timeout_seconds": self.timeout_seconds,
                    "last_heartbeat_at": self.last_heartbeat_at.isoformat(),
                },
            )

        return RuleEvaluation(
            rule_name="HeartbeatRule",
            passed=True,
            severity=RuleSeverity.INFO,
            message=f"Agent healthy; last heartbeat was {int(elapsed)}s ago",
            details={
                "elapsed_seconds": elapsed,
                "last_heartbeat_at": self.last_heartbeat_at.isoformat(),
            },
        )


class WatchdogRuleEngine:
    """Orchestrates all watchdog safety rules."""

    def __init__(
        self,
        spend_cap_usd: float = 20.0,
        spend_warning_usd: float = 16.0,
        max_incidents_per_hour: int = 10,
        max_pod_deletions_per_incident: int = 5,
        heartbeat_timeout_seconds: int = 300,
    ) -> None:
        self.spend_rule = SpendCapRule(
            spend_cap_usd=spend_cap_usd,
            warning_threshold_usd=spend_warning_usd,
        )
        self.rate_limit_rule = RateLimitRule(
            max_incidents_per_hour=max_incidents_per_hour,
        )
        self.scope_rule = ScopeBoundaryRule()
        self.blast_radius_rule = BlastRadiusRule(
            max_pod_deletions_per_incident=max_pod_deletions_per_incident,
        )
        self.heartbeat_rule = HeartbeatRule(
            timeout_seconds=heartbeat_timeout_seconds,
        )

    def evaluate_periodic_health(
        self,
        current_spend_usd: float,
        current_time: datetime | None = None,
    ) -> list[RuleEvaluation]:
        """Run periodic rules evaluated during the health check loop."""
        now = current_time or datetime.now(UTC)
        return [
            self.spend_rule.evaluate(current_spend_usd),
            self.rate_limit_rule.evaluate(now),
            self.heartbeat_rule.evaluate(now),
        ]

    @staticmethod
    def has_critical_violation(evaluations: list[RuleEvaluation]) -> bool:
        """Check whether any evaluated rule produced a critical violation."""
        return any(
            not ev.passed and ev.severity == RuleSeverity.VIOLATION
            for ev in evaluations
        )
