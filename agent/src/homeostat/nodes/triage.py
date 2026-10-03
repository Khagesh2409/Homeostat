"""
Triage node — the first stop after an alert arrives.

Responsibilities:
  1. Parse the raw alert payload into a structured Alert
  2. Assign a failure_signature (the runbook lookup key)
  3. Decide whether Tier-0 can handle this (is_tier0)
  4. Log the incident start

This node makes NO LLM calls. It is purely deterministic.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from homeostat.state import AgentMode, AgentState, Alert, IncidentOutcome
from homeostat.tier0.registry import is_tier0_candidate

logger = logging.getLogger(__name__)


def triage(state: AgentState) -> AgentState:
    """
    Parse the incoming alert, assign an incident ID, and decide routing.

    Input state keys:  raw_alert_payload (or current_alert already set)
    Output state keys: current_alert, incident_id, failure_signature, is_tier0, mode, incident_log
    """
    log: list[str] = list(state.get("incident_log", []))

    # Parse alert if it arrived as raw payload
    alert: Alert = state.get("current_alert")  # type: ignore[assignment]
    if alert is None:
        payload = state.get("raw_alert_payload", {})
        source = payload.get("source", "alertmanager")
        if source == "k8s_event":
            alert = Alert.from_event_payload(payload)
        else:
            alert = Alert.from_alertmanager_payload(payload)

    incident_id = state.get("incident_id") or str(uuid.uuid4())[:8]
    log.append(
        f"[{_now()}] TRIAGE: Incident {incident_id} — {alert.alertname} "
        f"on {alert.source} in {alert.namespace} (severity={alert.severity.value})"
    )

    # Build the failure signature — the key for runbook lookup
    # Format: "<normalized_source>:<alertname>"
    failure_signature = f"{alert.source}:{alert.alertname}"

    # Decide if Tier-0 can handle this (alertname gate; tier0 node does the full match)
    is_tier0 = is_tier0_candidate(alert)

    routing = "tier0" if is_tier0 else "memory_lookup"
    log.append(f"[{_now()}] TRIAGE: Routing to {routing} (failure_signature={failure_signature})")

    logger.info(
        "Triage complete: incident=%s alert=%s tier0=%s",
        incident_id, alert.alertname, is_tier0,
    )

    return {
        "current_alert": alert,
        "incident_id": incident_id,
        "failure_signature": failure_signature,
        "is_tier0": is_tier0,
        "tier0_playbook_name": "",
        "tier0_miss": False,
        "mode": AgentMode.INCIDENT,
        "retry_count": state.get("retry_count", 0),
        "max_retries": state.get("max_retries", 3),
        "cost_usd": state.get("cost_usd", 0.0),
        "token_count": state.get("token_count", 0),
        "llm_calls": state.get("llm_calls", 0),
        "outcome": IncidentOutcome.IN_PROGRESS,
        "incident_log": log,
    }


def route_after_triage(state: AgentState) -> str:
    """LangGraph routing function — called after triage to decide next node."""
    if state.get("is_tier0"):
        return "tier0"
    return "memory_lookup"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
