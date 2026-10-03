"""
Runbook and state storage schemas.

These are the data structures persisted in DynamoDB and S3.
Keyed on structured failure signatures — never raw prose.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ──────────────────────────────────────────────────────────────
# Failure Signature — the runbook's lookup key
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FailureSignature:
    """
    Structured key that identifies a class of failure.

    Designed so that the same type of failure always produces the same key,
    regardless of which specific pod or timestamp was involved.

    Examples:
        "pod:CrashLoopBackOff:deployment/nginx:a3f9"
        "disk:DiskPressure:node/*:0000"
        "network:TargetDown:service/api:b7c2"
    """

    source_type: str  # "pod" | "node" | "service" | "disk" | "network"
    error_category: str  # "CrashLoopBackOff" | "OOMKilled" | "DiskPressure" | ...
    affected_resource: str  # normalized: "deployment/nginx", "node/*", "service/api"
    context_hash: str  # short hash of relevant context (namespace, labels)

    @classmethod
    def make(
        cls,
        source_type: str,
        error_category: str,
        affected_resource: str,
        context: dict[str, Any] | None = None,
    ) -> FailureSignature:
        """
        Build a FailureSignature, computing the context_hash automatically.

        Args:
            source_type: Type of affected resource
            error_category: The error type
            affected_resource: Normalized resource name (strip specific pod names)
            context: Dict of context values to hash (e.g. namespace, labels)
        """
        # Normalize the resource: strip specific pod hash suffixes
        # "nginx-7d9f8b-xkqp2" → "deployment/nginx" if we know the owner
        # But if we don't, keep the type prefix at least
        normalized = affected_resource.lower().strip()

        # Compute a short hash of context for extra discrimination
        ctx_str = str(sorted((context or {}).items()))
        ctx_hash = hashlib.sha256(ctx_str.encode()).hexdigest()[:4]

        return cls(
            source_type=source_type.lower(),
            error_category=error_category,
            affected_resource=normalized,
            context_hash=ctx_hash,
        )

    @classmethod
    def from_str(cls, key: str) -> FailureSignature:
        """Parse a colon-separated key back into a FailureSignature."""
        parts = key.split(":", 3)
        if len(parts) < 4:
            raise ValueError(f"Invalid FailureSignature key: {key}")
        return cls(
            source_type=parts[0].lower(),
            error_category=parts[1],
            affected_resource=parts[2].lower(),
            context_hash=parts[3],
        )

    @property
    def key(self) -> str:
        """The DynamoDB partition key."""
        return (
            f"{self.source_type}:{self.error_category}:"
            f"{self.affected_resource}:{self.context_hash}"
        )

    def __str__(self) -> str:
        return self.key


# ──────────────────────────────────────────────────────────────
# Action Step — a single step in a recovery plan
# ──────────────────────────────────────────────────────────────


@dataclass
class ActionStep:
    """One step in a recovery plan."""

    tool: str  # "kubectl" | "terraform" | "helm" | "system"
    command: str = ""  # The operation / command to execute
    action: str = ""  # Backward-compatible alias for command
    args: dict[str, Any] = field(default_factory=dict)
    dry_run: bool | None = None  # always dry-run first
    dry_run_safe: bool | None = None  # alias for dry_run
    description: str = ""  # human readable description

    def __post_init__(self) -> None:
        if not self.command and self.action:
            self.command = self.action
        elif not self.action and self.command:
            self.action = self.command
        if self.dry_run is None and self.dry_run_safe is None:
            self.dry_run = True
            self.dry_run_safe = True
        elif self.dry_run is not None and self.dry_run_safe is None:
            self.dry_run_safe = self.dry_run
        elif self.dry_run_safe is not None and self.dry_run is None:
            self.dry_run = self.dry_run_safe
        else:
            self.dry_run = bool(self.dry_run and self.dry_run_safe)
            self.dry_run_safe = self.dry_run


# ──────────────────────────────────────────────────────────────
# Runbook — the learned recovery procedure
# ──────────────────────────────────────────────────────────────


@dataclass
class Runbook:
    """
    A learned recovery procedure for a specific failure signature.

    Written by the agent after a successful recovery.
    Retrieved on the next occurrence and validated with a discriminating_check
    before being applied.
    """

    failure_signature: FailureSignature
    version: int

    # What the LLM diagnosed
    diagnosis: str

    # A cheap, fast check to confirm this runbook is appropriate
    # before acting. e.g. "verify DNS resolution is failing"
    # If this check fails, discard the runbook and re-diagnose fresh.
    discriminating_check: str

    # The recovery plan
    action_plan: list[ActionStep]

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Learning metrics — updated on every use
    times_used: int = 0
    times_succeeded: int = 0
    times_failed: int = 0
    avg_recovery_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.times_used == 0:
            return 0.0
        return self.times_succeeded / self.times_used

    @property
    def confidence(self) -> float:
        """
        Weighted confidence score.
        Low usage = low confidence even with high success rate.
        Converges toward success_rate as usage grows.
        """
        weight = min(self.times_used / 10.0, 1.0)
        return self.success_rate * weight

    def to_dynamodb_item(self) -> dict[str, Any]:
        """Serialize to DynamoDB item format."""
        return {
            "failure_signature": self.failure_signature.key,
            "version": self.version,
            "diagnosis": self.diagnosis,
            "discriminating_check": self.discriminating_check,
            "action_plan": json.dumps([
                {
                    "tool": s.tool,
                    "action": s.action or s.command,
                    "command": s.command or s.action,
                    "args": s.args,
                    "dry_run": s.dry_run,
                    "dry_run_safe": s.dry_run_safe,
                    "description": s.description,
                }
                for s in self.action_plan
            ]),
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat(),
            "times_used": self.times_used,
            "times_succeeded": self.times_succeeded,
            "times_failed": self.times_failed,
            "avg_recovery_ms": int(self.avg_recovery_ms),
            # TTL: expire after 1 year of no use
            "expires_at": int((self.last_used.timestamp()) + (365 * 24 * 3600)),
        }

    @classmethod
    def from_dynamodb_item(cls, item: dict[str, Any]) -> Runbook:
        """Deserialize from DynamoDB item format."""
        sig = FailureSignature.from_str(item["failure_signature"])
        action_plan = [
            ActionStep(
                tool=s["tool"],
                command=s.get("command", s.get("action", "")),
                action=s.get("action", s.get("command", "")),
                args=s.get("args", {}),
                dry_run=s.get("dry_run", True),
                dry_run_safe=s.get("dry_run_safe", True),
                description=s.get("description", ""),
            )
            for s in json.loads(item["action_plan"])
        ]
        return cls(
            failure_signature=sig,
            version=int(item["version"]),
            diagnosis=item["diagnosis"],
            discriminating_check=item["discriminating_check"],
            action_plan=action_plan,
            created_at=datetime.fromisoformat(item["created_at"]),
            last_used=datetime.fromisoformat(item["last_used"]),
            times_used=int(item.get("times_used", 0)),
            times_succeeded=int(item.get("times_succeeded", 0)),
            times_failed=int(item.get("times_failed", 0)),
            avg_recovery_ms=float(item.get("avg_recovery_ms", 0)),
        )
