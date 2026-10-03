"""
Execute node — takes real action on the cluster.

Executes each step in the plan.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import AgentState

logger = logging.getLogger(__name__)


def execute(state: AgentState) -> AgentState:
    """
    Execute the validated plan.

    Input state keys:  plan
    Output state keys: actions_taken, incident_log
    """
    from homeostat.tools.executor import execute_step

    log = list(state.get("incident_log", []))
    plan = state.get("plan")

    if not plan or not plan.steps:
        log.append(f"[{_now()}] EXECUTE: No plan to execute")
        return {"incident_log": log}

    log.append(f"[{_now()}] EXECUTE: Taking action ({len(plan.steps)} steps)")
    actions_taken = list(state.get("actions_taken", []))

    for step in plan.steps:
        log.append(f"[{_now()}] EXECUTE: Running {step.command}")
        result = execute_step(step, dry_run=False)
        actions_taken.append(result)

        if not result.success:
            log.append(f"[{_now()}] EXECUTE: ✗ Step failed: {result.error}")
            logger.error("Execution failed on step %s: %s", step.command, result.error)
            # Stop execution on first failure
            break
        else:
            log.append(f"[{_now()}] EXECUTE: ✓ Step succeeded")

    return {
        "actions_taken": actions_taken,
        "incident_log": log,
    }


def route_after_execute(state: AgentState) -> str:
    """Always route to verify to check if the action worked."""
    return "verify"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
