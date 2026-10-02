"""
Escalate node — called when the agent exhausts all retries or hits an unrecoverable state.

Logs the failure and updates the state. In a real system, this would page a human.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import AgentState, IncidentOutcome

logger = logging.getLogger(__name__)


def escalate(state: AgentState) -> AgentState:
    """
    Escalate the incident to a human operator.

    Input state keys:  incident_id
    Output state keys: outcome, incident_log
    """
    log = list(state.get("incident_log", []))
    incident_id = state.get("incident_id", "unknown")

    log.append(f"[{_now()}] ESCALATE: Maximum retries exceeded or unrecoverable error.")
    log.append(f"[{_now()}] ESCALATE: 🚨 Paging human for incident {incident_id} 🚨")

    logger.critical("Escalated incident %s to human operator", incident_id)

    return {
        "outcome": IncidentOutcome.ESCALATED,
        "incident_log": log,
    }


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
