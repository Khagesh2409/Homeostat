"""
Runbook write-back and statistical learning.

Saves successful recovery plans as versioned runbooks or updates historical
metrics on existing runbooks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeostat.memory.client import RunbookClient
from homeostat.memory.schemas import ActionStep, FailureSignature, Runbook
from homeostat.state import ActionPlan

logger = logging.getLogger(__name__)


def _plans_equal(plan1: list[ActionStep], plan2: list[ActionStep]) -> bool:
    """Check if two action plans have identical steps and arguments."""
    if len(plan1) != len(plan2):
        return False
    for s1, s2 in zip(plan1, plan2, strict=True):
        if s1.tool != s2.tool:
            return False
        cmd1 = s1.command or s1.action
        cmd2 = s2.command or s2.action
        if cmd1 != cmd2:
            return False
        if s1.args != s2.args:
            return False
    return True


class RunbookWriter:
    """
    Handles write-back of recovered action plans to DynamoDB.

    - If no runbook existed -> creates version 1
    - If identical runbook existed -> increments success/usage stats
    - If plan changed/improved -> creates next version
    - If discriminating check failed -> logs but does not delete
    """

    def __init__(self, client: RunbookClient | None = None) -> None:
        self.client = client or RunbookClient()

    def write_runbook(
        self,
        signature: FailureSignature | str,
        diagnosis: str,
        action_plan: list[ActionStep] | ActionPlan | list[dict[str, Any]],
        discriminating_check: str,
        recovery_ms: float = 0.0,
    ) -> Runbook:
        """
        Save or update a runbook after successful incident recovery.

        Args:
            signature: FailureSignature or key string.
            diagnosis: The root-cause diagnosis.
            action_plan: Steps executed to recover.
            discriminating_check: Fast check to validate applicability in future.
            recovery_ms: Duration of recovery in milliseconds.
        """
        # 1. Normalize signature
        if isinstance(signature, str):
            if ":" in signature and len(signature.split(":")) >= 4:
                sig = FailureSignature.from_str(signature)
            else:
                sig = FailureSignature(
                    source_type="system",
                    error_category="General",
                    affected_resource=signature,
                    context_hash="0000",
                )
        else:
            sig = signature

        # 2. Normalize steps
        steps: list[ActionStep] = []
        if isinstance(action_plan, ActionPlan):
            for s in action_plan.steps:
                steps.append(
                    ActionStep(
                        tool=s.tool,
                        command=s.command,
                        action=s.command,
                        args=s.args,
                        dry_run=s.dry_run_safe,
                        dry_run_safe=s.dry_run_safe,
                        description=s.description,
                    )
                )
        elif isinstance(action_plan, list):
            for item in action_plan:
                if isinstance(item, ActionStep):
                    steps.append(item)
                elif isinstance(item, dict):
                    steps.append(
                        ActionStep(
                            tool=str(item.get("tool", "system")),
                            command=str(item.get("command", item.get("action", ""))),
                            action=str(item.get("action", item.get("command", ""))),
                            args=item.get("args", {}),
                            dry_run=bool(item.get("dry_run", True)),
                            dry_run_safe=bool(item.get("dry_run_safe", True)),
                            description=str(item.get("description", "")),
                        )
                    )

        # 3. Check for existing runbook
        existing = self.client.get_latest(sig)

        if existing is None:
            # Create Version 1
            new_runbook = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis=diagnosis,
                discriminating_check=discriminating_check,
                action_plan=steps,
                created_at=datetime.now(UTC),
                last_used=datetime.now(UTC),
                times_used=1,
                times_succeeded=1,
                times_failed=0,
                avg_recovery_ms=recovery_ms,
            )
            self.client.put(new_runbook)
            logger.info("Created new runbook v1 for signature: %s", sig.key)
            return new_runbook

        # Check if plan matches existing version
        if _plans_equal(existing.action_plan, steps):
            # Same plan worked again! Increment stats on current version
            self.client.record_outcome(
                sig,
                existing.version,
                success=True,
                recovery_ms=recovery_ms,
            )
            existing.times_used += 1
            existing.times_succeeded += 1
            existing.last_used = datetime.now(UTC)
            if existing.times_used > 1 and recovery_ms > 0:
                existing.avg_recovery_ms = (
                    (existing.avg_recovery_ms * (existing.times_used - 1)) + recovery_ms
                ) / existing.times_used
            logger.info(
                "Updated stats for runbook %s v%d (uses=%d, confidence=%.2f)",
                sig.key,
                existing.version,
                existing.times_used,
                existing.confidence,
            )
            return existing

        # Plan has changed or improved -> increment version
        next_ver = self.client.next_version(sig)
        new_version_runbook = Runbook(
            failure_signature=sig,
            version=next_ver,
            diagnosis=diagnosis,
            discriminating_check=discriminating_check,
            action_plan=steps,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            times_used=1,
            times_succeeded=1,
            times_failed=0,
            avg_recovery_ms=recovery_ms,
        )
        self.client.put(new_version_runbook)
        logger.info(
            "Created updated runbook v%d for signature: %s",
            next_ver,
            sig.key,
        )
        return new_version_runbook

    def record_failure(
        self,
        signature: FailureSignature | str,
        version: int,
        recovery_ms: float = 0.0,
    ) -> bool:
        """Record an execution failure for a runbook."""
        return self.client.record_outcome(
            signature,
            version,
            success=False,
            recovery_ms=recovery_ms,
        )

    def record_discriminating_check_failure(
        self,
        signature: FailureSignature | str,
        version: int,
        reason: str,
    ) -> None:
        """
        Record that a discriminating check failed for a runbook.

        Per design: log but do not delete, since the runbook may be valid
        under a different incident context.
        """
        sig_key = signature.key if isinstance(signature, FailureSignature) else str(signature)
        logger.warning(
            "Runbook %s v%d failed discriminating check: %s "
            "(preserved in memory for alternate contexts)",
            sig_key,
            version,
            reason,
        )
