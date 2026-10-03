"""
Tool executor — validates scope and dispatches ActionSteps to agent tools.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeostat.state import ActionResult, ActionStep
from homeostat.tools.helm import default_helm
from homeostat.tools.kubectl import default_kubectl
from homeostat.tools.memory import default_memory
from homeostat.tools.prometheus import default_prometheus
from homeostat.tools.scope import check_scope
from homeostat.tools.system import default_system
from homeostat.tools.terraform import default_terraform

logger = logging.getLogger(__name__)


def execute_step(step: ActionStep, dry_run: bool = False) -> ActionResult:
    """
    Execute a single ActionStep, or simulate it if dry_run=True.

    Enforces scope restrictions before any tool invocation.
    """
    now = datetime.now(UTC)

    # 1. Enforce scope restrictions
    scope_decision = check_scope(step.tool, step.command, step.args)
    if not scope_decision.allowed:
        logger.warning(
            "Scope violation blocked execution of %s:%s - %s",
            step.tool,
            step.command,
            scope_decision.reason,
        )
        return ActionResult(
            step=step,
            success=False,
            error=f"Scope violation: {scope_decision.reason}",
            output="",
            executed_at=now,
            dry_run=dry_run,
        )

    # 2. Check dry-run safety
    if dry_run and not step.dry_run_safe:
        logger.info("Skipping unsafe dry-run command: %s", step.command)
        return ActionResult(
            step=step,
            success=True,
            output=f"[dry-run] Step '{step.command}' marked not dry-run safe — simulated pass",
            executed_at=now,
            dry_run=True,
        )

    # 3. Dispatch to appropriate tool wrapper
    tool_key = step.tool.lower().removesuffix("_tool")
    res: dict[str, Any]

    try:
        if tool_key == "kubectl":
            res = default_kubectl.run_command(step.command, step.args, dry_run=dry_run)
        elif tool_key == "terraform":
            res = default_terraform.run_command(step.command, step.args, dry_run=dry_run)
        elif tool_key == "helm":
            res = default_helm.run_command(step.command, step.args, dry_run=dry_run)
        elif tool_key == "system":
            res = default_system.run_command(step.command, step.args, dry_run=dry_run)
        elif tool_key == "prometheus":
            res = default_prometheus.run_command(step.command, step.args, dry_run=dry_run)
        elif tool_key == "memory":
            res = default_memory.run_command(step.command, step.args, dry_run=dry_run)
        else:
            return ActionResult(
                step=step,
                success=False,
                error=f"Unknown tool: '{step.tool}'",
                output="",
                executed_at=now,
                dry_run=dry_run,
            )

        return ActionResult(
            step=step,
            success=bool(res.get("success", False)),
            output=str(res.get("output", "")),
            error=str(res.get("error", "")),
            executed_at=now,
            dry_run=dry_run,
        )

    except Exception as exc:
        logger.exception("Unexpected error executing step %s", step.command)
        return ActionResult(
            step=step,
            success=False,
            error=f"Execution error: {exc}",
            output="",
            executed_at=now,
            dry_run=dry_run,
        )
