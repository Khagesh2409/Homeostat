"""
Prometheus instrumentation and metric definitions for Homeostat.
"""

from __future__ import annotations

import logging

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

# ── Incident & Recovery Metrics ───────────────────────────────

INCIDENTS_TOTAL = Counter(
    "homeostat_incidents_total",
    "Total incidents processed by Homeostat",
    ["severity", "tier"],
)

RECOVERY_DURATION_SECONDS = Histogram(
    "homeostat_recovery_duration_seconds",
    "Time taken to resolve an incident in seconds",
    buckets=(5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0),
)

# ── LLM Token & Cost Metrics ──────────────────────────────────

LLM_TOKENS_TOTAL = Counter(
    "homeostat_llm_tokens_total",
    "Total LLM tokens consumed",
    ["model", "direction"],
)

LLM_COST_USD = Gauge(
    "homeostat_llm_cost_usd",
    "Total cumulative LLM spend in USD",
)

# ── Memory & Runbook Metrics ──────────────────────────────────

RUNBOOK_RETRIEVALS_TOTAL = Counter(
    "homeostat_runbook_retrievals_total",
    "Total runbook memory lookups",
    ["result"],  # "hit", "miss", "wrong"
)

# ── Tier-0 Playbook Metrics ───────────────────────────────────

TIER0_ACTIONS_TOTAL = Counter(
    "homeostat_tier0_actions_total",
    "Total Tier-0 deterministic playbook actions taken",
    ["playbook", "result"],  # result="success"|"failure"|"cooldown"
)

# ── Budget Enforcement Metrics ────────────────────────────────

BUDGET_REMAINING_USD = Gauge(
    "homeostat_budget_remaining_usd",
    "Remaining incident or monthly budget in USD",
)


# ── Helper recording functions ────────────────────────────────


def record_incident(
    severity: str,
    tier: str,
    duration_seconds: float | None = None,
) -> None:
    """Record an incident occurrence and optional recovery duration."""
    INCIDENTS_TOTAL.labels(severity=severity.lower(), tier=tier.lower()).inc()
    if duration_seconds is not None and duration_seconds > 0:
        RECOVERY_DURATION_SECONDS.observe(duration_seconds)


def record_llm_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None = None,
) -> None:
    """Record LLM token counts and updated cumulative cost."""
    if input_tokens > 0:
        LLM_TOKENS_TOTAL.labels(model=model, direction="input").inc(input_tokens)
    if output_tokens > 0:
        LLM_TOKENS_TOTAL.labels(model=model, direction="output").inc(output_tokens)
    if cost_usd is not None and cost_usd > 0:
        LLM_COST_USD.inc(cost_usd)


def record_runbook_retrieval(result: str) -> None:
    """Record runbook lookup outcome: 'hit', 'miss', or 'wrong'."""
    normalized = result.lower()
    if normalized not in ("hit", "miss", "wrong"):
        normalized = "miss"
    RUNBOOK_RETRIEVALS_TOTAL.labels(result=normalized).inc()


def record_tier0_action(playbook: str, result: str) -> None:
    """Record execution of a Tier-0 playbook."""
    TIER0_ACTIONS_TOTAL.labels(playbook=playbook, result=result.lower()).inc()


def set_budget_remaining(remaining_usd: float) -> None:
    """Set the current budget remaining gauge."""
    BUDGET_REMAINING_USD.set(max(0.0, remaining_usd))


def get_metrics_output(registry: CollectorRegistry = REGISTRY) -> bytes:
    """Export all registered Prometheus metrics as UTF-8 bytes."""
    return generate_latest(registry)
