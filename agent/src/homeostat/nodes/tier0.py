"""
Tier-0 node — executes deterministic playbooks for trivial incidents.

No LLM calls. Bypasses diagnosis and planning entirely.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import ActionPlan, AgentState
from homeostat.tier0.cooldown import default_tracker
from homeostat.tier0.registry import cooldown_key, resolve_playbook

logger = logging.getLogger(__name__)


def tier0(state: AgentState) -> AgentState:
    """
    Execute the deterministic Tier-0 playbook for this alert.

    Input state keys:  current_alert, error_signatures
    Output state keys: plan (hardcoded from playbook), is_tier0, tier0_playbook_name,
                       tier0_target, verify_steps, incident_log
    """
    log = list(state.get("incident_log", []))
    alert = state.get("current_alert")

    if not alert:
        log.append(f"[{_now()}] TIER0: No alert in state — routing to memory_lookup")
        return {"incident_log": log, "is_tier0": False}

    resolution = resolve_playbook(alert, state.get("error_signatures", []))
    match = resolution.match

    if match is None:
        # Triage thought it was tier0, but no playbook matched or it's cooling down
        log.append(f"[{_now()}] TIER0: {resolution.reason} — routing to memory_lookup")
        return {"is_tier0": False, "tier0_playbook_name": "", "incident_log": log}

    # Commit to acting: this counts against the playbook's cooldown and retry budget
    default_tracker.record_attempt(cooldown_key(match.name, match.target))
    log.append(f"[{_now()}] TIER0: {resolution.reason}")
    logger.info("Executing Tier-0 playbook: %s on %s", match.name, match.target)

    # Convert the playbook's actions into a standard ActionPlan so it can use the
    # exact same dry_run/execute nodes as LLM-generated plans
    plan = ActionPlan(
        steps=match.actions,
        rationale=f"Deterministic Tier-0 playbook: {match.playbook.description}",
        estimated_risk="low",
        generated_by="tier0",
    )

    return {
        "plan": plan,
        "tier0_playbook_name": match.name,
        "tier0_target": match.target,
        "verify_steps": match.verify_steps,
        "incident_log": log,
    }


def route_after_tier0(state: AgentState) -> str:
    """If Tier-0 handled it, go to dry_run. If it bailed, go to memory_lookup."""
    if state.get("tier0_playbook_name"):
        return "dry_run"
    return "memory_lookup"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
