"""
Validate node — cheap sanity check for a retrieved runbook before executing it.

Before blindly executing a stored runbook, we do a quick discriminating check:
  - Does the runbook's expected symptom match the current alert?
  - Is the runbook for the right resource type?

This prevents "wrong diagnosis" — finding a runbook for nginx crashes and
applying it to a postgres OOM kill.

No LLM call. Pure string matching.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import AgentState

logger = logging.getLogger(__name__)


def validate(state: AgentState) -> AgentState:
    """
    Validate that the retrieved runbook actually applies to the current incident.

    Input state keys:  retrieved_runbook, current_alert, failure_signature
    Output state keys: incident_log (+ routing decision embedded in state)
    """
    log = list(state.get("incident_log", []))
    runbook = state.get("retrieved_runbook", {})
    alert = state.get("current_alert")
    failure_signature = state.get("failure_signature", "")

    log.append(f"[{_now()}] VALIDATE: Checking runbook applicability")

    if not runbook or not alert:
        log.append(f"[{_now()}] VALIDATE: Missing runbook or alert — routing to diagnose")
        return {"incident_log": log, "retrieved_runbook": None}

    # Check 1: Does the runbook's failure signature match (at least the alertname)?
    runbook_sig = runbook.get("failure_signature", "")
    current_alertname = alert.alertname

    sig_matches = current_alertname in runbook_sig or runbook_sig in failure_signature

    # Check 2: Does the resource type match? (pod vs node vs deployment)
    runbook_source = runbook_sig.split(":")[0] if ":" in runbook_sig else ""
    current_source = alert.source
    # Normalize: "deployment/nginx" and "pod/nginx-abc" are the same resource type
    def _resource_type(src: str) -> str:
        return src.split("/")[0] if "/" in src else src

    type_matches = _resource_type(runbook_source) == _resource_type(current_source)

    if sig_matches and type_matches:
        log.append(
            f"[{_now()}] VALIDATE: Runbook is applicable "
            f"(sig_match={sig_matches}, type_match={type_matches}) — routing to execute"
        )
        logger.info("Runbook validation passed for %s", failure_signature)
        return {"incident_log": log}  # Keep retrieved_runbook as-is
    else:
        log.append(
            f"[{_now()}] VALIDATE: Runbook mismatch "
            f"(sig_match={sig_matches}, type_match={type_matches}) "
            "— discarding runbook, routing to diagnose"
        )
        logger.info(
            "Runbook validation failed for %s (sig=%s, type=%s)",
            failure_signature, sig_matches, type_matches
        )
        # Clear the runbook so diagnose knows to start fresh
        return {"retrieved_runbook": None, "runbook_confidence": 0.0, "incident_log": log}


def route_after_validate(state: AgentState) -> str:
    """Route to execute if runbook is valid, otherwise diagnose."""
    runbook = state.get("retrieved_runbook")
    if runbook:
        return "execute"
    return "diagnose"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
