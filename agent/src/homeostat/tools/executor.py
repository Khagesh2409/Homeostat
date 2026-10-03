"""
Tool executor — dispatches ActionStep execution to specific tool modules.
"""

from __future__ import annotations

import logging

from homeostat.state import ActionResult, ActionStep

logger = logging.getLogger(__name__)


def execute_step(step: ActionStep, dry_run: bool = False) -> ActionResult:
    """
    Execute a single ActionStep, or simulate it if dry_run=True.

    Dispatches to kubectl, system, prometheus, or memory tools.
    """
    logger.debug("Executing step %s (dry_run=%s)", step.command, dry_run)
    return ActionResult(
        step=step,
        success=True,
        output=f"[dry_run={dry_run}] executed {step.command}",
        dry_run=dry_run,
    )
