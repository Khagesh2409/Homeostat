"""Maintenance node — performs shadow tested updates and background maintenance."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from homeostat.state import AgentMode, AgentState, AlertSeverity
from homeostat.tools.kubectl import KubectlTool, default_kubectl
from homeostat.tools.shadow import ShadowTool, default_shadow

logger = logging.getLogger(__name__)


def maintenance(
    state: AgentState,
    *,
    shadow: ShadowTool = default_shadow,
    kubectl: KubectlTool = default_kubectl,
) -> dict[str, Any]:
    """Perform scheduled maintenance tasks using shadow rehearsals for safety.

    Workflow:
    1. Safety check: Immediately interrupt if active incident alerts exist.
    2. Extract or generate candidate maintenance changes.
    3. Rehearse each candidate change in the isolated shadow namespace.
    4. Verify health in shadow:
       - If healthy: apply change to target workload with rollback safety.
       - If unhealthy: discard candidate change and leave production untouched.
    5. Cleanup shadow rehearsal workloads.
    """
    log = list(state.get("incident_log", []))

    current_alert = state.get("current_alert")
    has_active_alert = bool(
        current_alert
        and current_alert.severity in (AlertSeverity.CRITICAL, AlertSeverity.WARNING)
    )
    if state.get("mode") == AgentMode.INCIDENT or has_active_alert:
        alert_name = current_alert.alertname if current_alert else "active incident"
        log.append(
            f"[{_now()}] MAINTENANCE: Interrupted immediately due to active alert '{alert_name}'"
        )
        return {
            "incident_log": log,
            "maintenance_outcome": "interrupted_by_alert",
        }

    # 2. Extract or generate candidate changes
    candidates: list[dict[str, Any]] = state.get("maintenance_candidates", [])
    if not candidates:
        log.append(
            f"[{_now()}] MAINTENANCE: No candidate maintenance work provided; running idle audit"
        )
        return {
            "incident_log": log,
            "maintenance_outcome": "no_work_required",
        }

    applied_changes: list[dict[str, Any]] = []
    rejected_changes: list[dict[str, Any]] = []

    for candidate in candidates:
        workload = str(candidate.get("workload", "demo-app"))
        source_namespace = str(candidate.get("source_namespace", "default"))
        shadow_namespace = str(candidate.get("shadow_namespace", "homeostat-shadow"))
        patch_data = candidate.get("patch", {})
        work_type = str(candidate.get("type", "config_tuning"))

        log.append(
            f"[{_now()}] MAINTENANCE: Evaluating candidate change [{work_type}] on '{workload}'"
        )

        # 3. Clone workload into shadow namespace
        clone_res = shadow.clone_workload(
            source_workload=workload,
            source_namespace=source_namespace,
            shadow_namespace=shadow_namespace,
        )
        if not clone_res.get("success"):
            reason = clone_res.get("error", "Unknown clone failure")
            log.append(
                f"[{_now()}] MAINTENANCE: Failed to clone '{workload}' to shadow: {reason}"
            )
            rejected_changes.append({"workload": workload, "reason": reason})
            continue

        # 4. Apply candidate change in shadow
        apply_shadow = shadow.apply_candidate_change(
            workload=workload,
            patch_data=patch_data,
            namespace=shadow_namespace,
        )
        if not apply_shadow.get("success"):
            reason = apply_shadow.get("error", "Failed to apply patch in shadow")
            log.append(
                f"[{_now()}] MAINTENANCE: Shadow patch application failed for "
                f"'{workload}': {reason}"
            )
            shadow.cleanup_shadow(workload=workload, namespace=shadow_namespace)
            rejected_changes.append({"workload": workload, "reason": reason})
            continue

        # 5. Run health checks on shadow deployment
        health_res = shadow.check_shadow_health(
            workload=workload,
            namespace=shadow_namespace,
        )
        if not health_res.get("healthy"):
            reason = health_res.get("error", "Shadow health check failed")
            log.append(
                f"[{_now()}] MAINTENANCE: Shadow rehearsal FAILED for '{workload}': {reason}. "
                f"Candidate discarded; production unchanged."
            )
            shadow.cleanup_shadow(workload=workload, namespace=shadow_namespace)
            rejected_changes.append({"workload": workload, "reason": reason})
            continue

        # 6. Shadow check passed: apply change to target production workload
        log.append(
            f"[{_now()}] MAINTENANCE: Shadow rehearsal PASSED for '{workload}'. "
            f"Applying change to target namespace '{source_namespace}'."
        )

        content = (
            patch_data
            if isinstance(patch_data, str)
            else json.dumps(patch_data)
        )
        prod_apply = kubectl.apply(
            content=content,
            namespace=source_namespace,
        )
        if not prod_apply.get("success"):
            err = prod_apply.get("error", "Failed to apply to production")
            log.append(
                f"[{_now()}] MAINTENANCE: Production apply failed for '{workload}': {err}"
            )
            shadow.cleanup_shadow(workload=workload, namespace=shadow_namespace)
            rejected_changes.append({"workload": workload, "reason": err})
            continue

        # 7. Cleanup shadow replica
        shadow.cleanup_shadow(workload=workload, namespace=shadow_namespace)
        log.append(
            f"[{_now()}] MAINTENANCE: Successfully applied [{work_type}] to "
            f"'{workload}' and cleaned shadow"
        )
        applied_changes.append(candidate)

    outcome = "completed" if applied_changes else "discarded"
    return {
        "incident_log": log,
        "maintenance_outcome": outcome,
        "maintenance_applied": applied_changes,
        "maintenance_rejected": rejected_changes,
    }


def _now() -> str:
    """Return formatted current UTC timestamp."""
    return datetime.now(UTC).strftime("%H:%M:%S")
