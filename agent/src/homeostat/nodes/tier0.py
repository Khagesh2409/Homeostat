"""
Tier-0 node — executes deterministic playbooks for trivial incidents.

No LLM calls. Bypasses diagnosis and planning entirely.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import ActionPlan, AgentState

logger = logging.getLogger(__name__)


def tier0(state: AgentState) -> AgentState:
    """
    Execute the deterministic Tier-0 playbook for this alert.

    Input state keys:  current_alert
    Output state keys: plan (hardcoded from playbook), is_tier0, tier0_playbook_name, incident_log
    """
    from homeostat.tier0.registry import get_playbook  # type: ignore[import-untyped]

    log = list(state.get("incident_log", []))
    alert = state.get("current_alert")

    if not alert:
        log.append(f"[{_now()}] TIER0: No alert in state — routing to diagnose")
        return {"incident_log": log, "is_tier0": False}

    # Find a matching playbook
    playbook = get_playbook(alert)

    if playbook:
        log.append(f"[{_now()}] TIER0: Matched playbook '{playbook.name}'")
        logger.info("Executing Tier-0 playbook: %s", playbook.name)

        # Convert the playbook's actions into a standard ActionPlan so it can use the
        # exact same execute node as LLM-generated plans
        plan = ActionPlan(
            steps=playbook.actions,
            rationale=f"Deterministic Tier-0 playbook: {playbook.name}",
            estimated_risk="low",
            generated_by="tier0",
        )

        return {
            "plan": plan,
            "tier0_playbook_name": playbook.name,
            "incident_log": log,
        }
    else:
        # Triage thought it was tier0, but no playbook actually matched the specific conditions
        log.append(f"[{_now()}] TIER0: No playbook matched conditions — routing to memory_lookup")
        return {"is_tier0": False, "incident_log": log}


def route_after_tier0(state: AgentState) -> str:
    """If Tier-0 handled it, go to dry_run (or execute). If it bailed, go to memory_lookup."""
    if state.get("tier0_playbook_name"):
        return "dry_run"
    return "memory_lookup"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
