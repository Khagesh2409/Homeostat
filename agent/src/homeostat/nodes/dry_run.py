"""
Dry-run node — validates an ActionPlan without side effects.

Iterates over each ActionStep and executes it in dry-run mode.
If all steps pass, routes to execute. If any step fails, routes back to diagnose.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeostat.state import AgentState, DryRunResult

logger = logging.getLogger(__name__)


def dry_run(state: AgentState) -> AgentState:
    """
    Dry-run the generated plan.

    Input state keys:  plan
    Output state keys: dry_run_result, incident_log
    """
    from homeostat.tools.executor import execute_step  # type: ignore[import-untyped]

    log = list(state.get("incident_log", []))
    plan = state.get("plan")

    if not plan or not plan.steps:
        log.append(f"[{_now()}] DRY_RUN: No plan to dry-run — routing to escalate")
        return {"incident_log": log}

    log.append(f"[{_now()}] DRY_RUN: Simulating {len(plan.steps)} steps")

    results = []
    all_passed = True
    failure_reason = ""

    for step in plan.steps:
        if not step.dry_run_safe:
            log.append(f"[{_now()}] DRY_RUN: Skipping {step.tool} (not dry-run safe)")
            continue

        result = execute_step(step, dry_run=True)
        results.append(result)

        if not result.success:
            all_passed = False
            failure_reason = f"Step '{step.command}' failed: {result.error}"
            log.append(f"[{_now()}] DRY_RUN: ✗ {step.command} failed")
            break
        else:
            log.append(f"[{_now()}] DRY_RUN: ✓ {step.command} passed")

    dry_run_result = DryRunResult(
        plan=plan,
        all_passed=all_passed,
        results=results,
        failure_reason=failure_reason,
    )

    if all_passed:
        logger.info("Dry-run passed for %d steps", len(plan.steps))
    else:
        logger.warning("Dry-run failed: %s", failure_reason)

    return {
        "dry_run_result": dry_run_result,
        "incident_log": log,
    }


def route_after_dry_run(state: AgentState) -> str:
    """Route to execute if dry-run passed, else re-diagnose (or escalate if max retries)."""
    dry_run_res = state.get("dry_run_result")
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    if not dry_run_res:
        return "escalate"

    if dry_run_res.all_passed:
        return "execute"

    if retry_count >= max_retries:
        return "escalate"

    # We can increment retry count here or in a dedicated retry node.
    # We'll rely on LangGraph to increment it if we loop back, but doing it explicitly is safer.
    return "diagnose"


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
