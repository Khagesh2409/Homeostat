"""
Write Runbook node — persists a successful recovery plan.

When an LLM-generated plan successfully fixes an issue, we save it
as a runbook so the next time this failure signature is seen, we can
skip the LLM and execute it deterministically.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import AgentState, IncidentOutcome

logger = logging.getLogger(__name__)


def write_runbook(state: AgentState) -> AgentState:
    """
    Save the successful plan to DynamoDB.

    Input state keys:  failure_signature, plan, current_alert, diagnosis
    Output state keys: outcome, incident_log
    """
    log = list(state.get("incident_log", []))
    plan = state.get("plan")
    signature = state.get("failure_signature")

    # If this was Tier-0, we don't write a new runbook
    if state.get("is_tier0"):
        log.append(f"[{_now()}] WRITE_RUNBOOK: Tier-0 handled this natively, skipping write")
        return {"outcome": IncidentOutcome.RESOLVED, "incident_log": log}

    # If we executed an existing runbook or LLM plan, persist or update via RunbookWriter
    if plan and signature:
        try:
            from homeostat.memory.writer import RunbookWriter

            writer = RunbookWriter()
            saved = writer.write_runbook(
                signature=signature,
                diagnosis=state.get("diagnosis", "Unknown diagnosis"),
                action_plan=plan,
                discriminating_check=plan.rationale,
            )
            log.append(
                f"[{_now()}] WRITE_RUNBOOK: ✓ Persisted runbook v{saved.version} for {signature}"
            )
            logger.info("Persisted runbook v%d for signature: %s", saved.version, signature)
        except Exception as exc:
            log.append(f"[{_now()}] WRITE_RUNBOOK: ✗ Failed to save runbook: {exc}")
            logger.error("Failed to save runbook: %s", exc)

    return {
        "outcome": IncidentOutcome.RESOLVED,
        "incident_log": log,
    }


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
