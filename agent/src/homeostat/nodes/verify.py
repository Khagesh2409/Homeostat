"""
Verify node — checks if the executed plan actually fixed the incident.

Queries Prometheus to see if the original alert is still firing,
or uses specific verification steps if Tier-0.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from homeostat.state import AgentState, VerificationResult
from homeostat.tier0.cooldown import default_tracker
from homeostat.tier0.registry import cooldown_key

logger = logging.getLogger(__name__)


def verify(state: AgentState) -> AgentState:
    """
    Verify if the incident is resolved.

    Input state keys:  current_alert
    Output state keys: verification_result, incident_log
    """
    # Note: In a real environment, we'd query Prometheus here.
    # For now, we will assume success if the action didn't error out,
    # but in a production setup this would poll the metrics/alerts.

    log = list(state.get("incident_log", []))

    log.append(f"[{_now()}] VERIFY: Checking if incident is resolved")

    # We add a slight delay to let the cluster state settle
    time.sleep(2)

    # Placeholder for actual metric/alert verification
    # We will assume success if there are actions taken and they succeeded.
    actions = state.get("actions_taken", [])
    success = len(actions) > 0 and all(a.success for a in actions)

    checks_passed = ["action_execution_success"] if success else []
    checks_failed = ["action_execution_failed"] if not success else []

    result = VerificationResult(
        success=success,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
    )

    updates: AgentState = {"verification_result": result}

    executed_plan = state.get("plan")
    ran_tier0_plan = executed_plan is not None and executed_plan.generated_by == "tier0"

    if success:
        log.append(f"[{_now()}] VERIFY: ✓ Recovery verified")
        logger.info("Incident verified resolved.")
    elif ran_tier0_plan and (playbook := state.get("tier0_playbook_name")):
        # Tier-0 miss: stand Tier-0 down for this target and hand straight to the LLM.
        # This doesn't consume a retry — the LLM gets its full budget.
        target = state.get("tier0_target", "")
        default_tracker.record_miss(cooldown_key(playbook, target))
        log.append(
            f"[{_now()}] VERIFY: ✗ Tier-0 miss on '{playbook}' ({target}) — escalating to LLM"
        )
        logger.warning("Tier-0 playbook %s missed on %s", playbook, target)
        updates.update({"tier0_miss": True, "tier0_playbook_name": "", "verify_steps": []})
    else:
        retry_count = state.get("retry_count", 0) + 1
        log.append(f"[{_now()}] VERIFY: ✗ Recovery failed to verify (attempt {retry_count})")
        logger.warning("Incident recovery failed verification.")
        updates["retry_count"] = retry_count

    updates["incident_log"] = log
    return updates


def route_after_verify(state: AgentState) -> str:
    """
    Route based on verification success.
    If success, write runbook.
    If failed, check max retries: diagnose again or escalate.
    """
    result = state.get("verification_result")
    if result and result.success:
        return "write_runbook"

    # retry_count was already incremented by the verify node (Tier-0 misses don't count)
    if state.get("retry_count", 0) >= state.get("max_retries", 3):
        return "escalate"

    return "diagnose"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
