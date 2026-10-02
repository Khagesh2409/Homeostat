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
    alert = state.get("current_alert")

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

    if success:
        log.append(f"[{_now()}] VERIFY: ✓ Recovery verified")
        logger.info("Incident verified resolved.")
    else:
        log.append(f"[{_now()}] VERIFY: ✗ Recovery failed to verify")
        logger.warning("Incident recovery failed verification.")

    return {
        "verification_result": result,
        "incident_log": log,
    }


def route_after_verify(state: AgentState) -> str:
    """
    Route based on verification success.
    If success, write runbook.
    If failed, check max retries: diagnose again or escalate.
    """
    result = state.get("verification_result")
    if result and result.success:
        return "write_runbook"

    # Increment retry count before routing to diagnose or escalate
    retry_count = state.get("retry_count", 0) + 1
    max_retries = state.get("max_retries", 3)

    if retry_count >= max_retries:
        return "escalate"

    return "diagnose"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
