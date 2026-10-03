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
from typing import Any

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
    parts = runbook_sig.split(":")
    if len(parts) >= 4:
        runbook_resource = parts[2]
        runbook_source_type = parts[0]
    else:
        runbook_resource = parts[0]
        runbook_source_type = parts[0]

    current_source = alert.source

    def _resource_type(src: str) -> str:
        return src.split("/")[0] if "/" in src else src

    current_type = _resource_type(current_source)
    type_matches = (
        _resource_type(runbook_resource) == current_type
        or _resource_type(runbook_source_type) == current_type
        or (
            runbook_source_type == "pod"
            and current_type in {"pod", "deployment", "statefulset", "daemonset"}
        )
    )

    # Check 3: Discriminating check confirmation
    disc_passed = True
    disc_reason = ""
    discriminating_check = runbook.get("discriminating_check", "").strip()
    if discriminating_check:
        from homeostat.memory.retrieval import RunbookRetriever
        from homeostat.memory.schemas import FailureSignature, Runbook

        retriever = RunbookRetriever()
        context = {
            "alertname": alert.alertname,
            "source": alert.source,
            "namespace": alert.namespace,
            "severity": str(alert.severity),
            "message": alert.message,
        }
        if alert.labels:
            context.update(alert.labels)
        state_dict: dict[str, Any] = state  # type: ignore[assignment]
        for key in ("restart_count", "restarts", "dns_failing", "symptom"):
            if key in state_dict:
                context[key] = state_dict[key]
        temp_sig = (
            FailureSignature.from_str(runbook_sig)
            if ":" in runbook_sig and len(runbook_sig.split(":")) >= 4
            else FailureSignature("pod", alert.alertname, alert.source, "0000")
        )
        temp_rb = Runbook(
            failure_signature=temp_sig,
            version=int(runbook.get("version", 1)),
            diagnosis=runbook.get("diagnosis", ""),
            discriminating_check=discriminating_check,
            action_plan=[],
        )
        disc_passed, disc_reason = retriever.confirm_discriminating_check(temp_rb, context)

    if sig_matches and type_matches and disc_passed:
        log.append(
            f"[{_now()}] VALIDATE: Runbook is applicable "
            f"(sig_match={sig_matches}, type_match={type_matches}) — routing to execute"
        )
        logger.info("Runbook validation passed for %s", failure_signature)
        return {"incident_log": log}  # Keep retrieved_runbook as-is
    else:
        reason_desc = (
            f"sig_match={sig_matches}, type_match={type_matches}"
            if (not sig_matches or not type_matches)
            else f"discriminating_check failed ({disc_reason})"
        )
        log.append(
            f"[{_now()}] VALIDATE: Runbook mismatch ({reason_desc}) "
            "— discarding runbook, routing to diagnose"
        )
        logger.info(
            "Runbook validation failed for %s (%s)",
            failure_signature,
            reason_desc,
        )
        # Log discriminating check rejection if applicable
        if sig_matches and type_matches and not disc_passed:
            from homeostat.memory.writer import RunbookWriter

            RunbookWriter().record_discriminating_check_failure(
                runbook_sig, int(runbook.get("version", 1)), disc_reason
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
