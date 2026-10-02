"""
The compiled LangGraph state machine.

Wires all nodes together according to the Homeostat design.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from homeostat.nodes import (
    diagnose,
    dry_run,
    escalate,
    execute,
    maintenance,
    memory_lookup,
    plan,
    route_after_dry_run,
    route_after_execute,
    route_after_memory_lookup,
    route_after_tier0,
    route_after_triage,
    route_after_validate,
    route_after_verify,
    tier0,
    triage,
    validate,
    verify,
    write_runbook,
)
from homeostat.state import AgentState

logger = logging.getLogger(__name__)


from langgraph.graph.state import CompiledStateGraph


def build_graph() -> CompiledStateGraph[Any, Any, Any]:
    """Build and compile the Homeostat agent graph."""

    workflow: StateGraph[Any, Any, Any, Any] = StateGraph(AgentState)

    # 1. Add all nodes
    workflow.add_node("triage", triage)
    workflow.add_node("tier0", tier0)
    workflow.add_node("memory_lookup", memory_lookup)
    workflow.add_node("validate", validate)
    workflow.add_node("diagnose", diagnose)
    workflow.add_node("plan", plan)
    workflow.add_node("dry_run", dry_run)
    workflow.add_node("execute", execute)
    workflow.add_node("verify", verify)
    workflow.add_node("write_runbook", write_runbook)
    workflow.add_node("escalate", escalate)
    workflow.add_node("maintenance", maintenance)

    # 2. Define edges and conditional routing

    # Entry point is always triage
    workflow.set_entry_point("triage")

    # Triage -> [Tier0 | MemoryLookup]
    workflow.add_conditional_edges(
        "triage",
        route_after_triage,
        {"tier0": "tier0", "memory_lookup": "memory_lookup"},
    )

    # Tier0 -> [DryRun | MemoryLookup]
    # (If Tier0 matches a playbook, go to dry_run. If it bails, fall back to memory_lookup)
    workflow.add_conditional_edges(
        "tier0",
        route_after_tier0,
        {"dry_run": "dry_run", "memory_lookup": "memory_lookup"},
    )

    # MemoryLookup -> [Validate | Diagnose]
    workflow.add_conditional_edges(
        "memory_lookup",
        route_after_memory_lookup,
        {"validate": "validate", "diagnose": "diagnose"},
    )

    # Validate -> [Execute | Diagnose]
    workflow.add_conditional_edges(
        "validate",
        route_after_validate,
        {"execute": "execute", "diagnose": "diagnose"},
    )

    # Diagnose -> Plan
    workflow.add_edge("diagnose", "plan")

    # Plan -> DryRun
    workflow.add_edge("plan", "dry_run")

    # DryRun -> [Execute | Diagnose | Escalate]
    workflow.add_conditional_edges(
        "dry_run",
        route_after_dry_run,
        {"execute": "execute", "diagnose": "diagnose", "escalate": "escalate"},
    )

    # Execute -> Verify
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {"verify": "verify"},
    )

    # Verify -> [WriteRunbook | Diagnose | Escalate]
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {"write_runbook": "write_runbook", "diagnose": "diagnose", "escalate": "escalate"},
    )

    # Terminating edges
    workflow.add_edge("write_runbook", END)
    workflow.add_edge("escalate", END)
    workflow.add_edge("maintenance", END)

    # Compile the graph
    app = workflow.compile()

    logger.info("Compiled LangGraph state machine.")
    return app
