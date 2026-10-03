"""
Memory lookup node — retrieves a runbook from DynamoDB if one exists.

If a runbook is found with sufficient confidence, route to validate.
If not found, route to diagnose (LLM cold start).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from homeostat.state import AgentState

logger = logging.getLogger(__name__)

# Minimum confidence to use a retrieved runbook (skip to validate)
_MIN_CONFIDENCE = 0.6


def memory_lookup(state: AgentState) -> AgentState:
    """
    Look up the failure_signature in DynamoDB runbook store.

    Input state keys:  failure_signature
    Output state keys: retrieved_runbook, runbook_confidence, incident_log
    """
    from homeostat.memory.client import RunbookClient

    log = list(state.get("incident_log", []))
    failure_signature = state.get("failure_signature", "")

    log.append(f"[{_now()}] MEMORY: Looking up runbook for '{failure_signature}'")

    runbook = None
    confidence = 0.0

    try:
        client = RunbookClient()
        runbook = client.get_latest_runbook(failure_signature)
        if runbook:
            confidence = _compute_confidence(runbook)
            log.append(
                f"[{_now()}] MEMORY: Found runbook (confidence={confidence:.2f}, "
                f"success_count={runbook.get('success_count', 0)}, "
                f"fail_count={runbook.get('fail_count', 0)})"
            )
            logger.info("Runbook found for %s (confidence=%.2f)", failure_signature, confidence)
        else:
            log.append(f"[{_now()}] MEMORY: No runbook found — routing to diagnose")
            logger.info("No runbook found for %s", failure_signature)
    except Exception as exc:
        logger.warning("Runbook lookup failed: %s — routing to diagnose", exc)
        log.append(f"[{_now()}] MEMORY: Lookup error ({exc}) — routing to diagnose")

    return {
        "retrieved_runbook": runbook,
        "runbook_confidence": confidence,
        "incident_log": log,
    }


def route_after_memory_lookup(state: AgentState) -> str:
    """Route to validate if we found a good runbook, otherwise diagnose."""
    runbook = state.get("retrieved_runbook")
    confidence = state.get("runbook_confidence", 0.0)
    if runbook and confidence >= _MIN_CONFIDENCE:
        return "validate"
    return "diagnose"


def _compute_confidence(runbook: dict[str, Any]) -> float:
    """
    Compute confidence score for a retrieved runbook.

    Simple heuristic based on historical success/fail ratio.
    Adds a Laplace smoothing term so a brand-new runbook starts at ~0.5.
    """
    success = runbook.get("success_count", 0)
    fail = runbook.get("fail_count", 0)
    total = success + fail
    if total == 0:
        return 0.5  # New runbook, uncertain
    # Laplace smoothing with alpha=1
    return float((success + 1) / (total + 2))


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
