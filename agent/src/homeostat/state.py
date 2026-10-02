"""
Agent state definitions — the single source of truth for what flows through the graph.

Every node reads from and writes to AgentState. Nothing else is passed between nodes.
Keeping all state here makes the graph inspectable, checkpointable, and testable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from homeostat.preprocessor.schemas import ErrorSignature

# ── Enums ─────────────────────────────────────────────────────────────────────


class AgentMode(str, Enum):
    IDLE = "idle"
    INCIDENT = "incident"
    MAINTENANCE = "maintenance"


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class IncidentOutcome(str, Enum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    IN_PROGRESS = "in_progress"


# ── Input types ───────────────────────────────────────────────────────────────


@dataclass
class Alert:
    """
    An incoming alert from Alertmanager or a K8s event from the event watcher.

    This is the raw incoming trigger — the agent's entry point.
    """
    alertname: str
    severity: AlertSeverity
    namespace: str
    source: str                       # "pod/nginx-abc", "node/*", "deployment/api"
    message: str
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    @classmethod
    def from_alertmanager_payload(cls, payload: dict[str, Any]) -> Alert:
        """Parse an Alertmanager webhook payload into an Alert."""
        labels = payload.get("labels", {})
        annotations = payload.get("annotations", {})
        return cls(
            alertname=labels.get("alertname", "Unknown"),
            severity=AlertSeverity(labels.get("severity", "warning")),
            namespace=labels.get("namespace", "default"),
            source=labels.get("pod", labels.get("deployment", labels.get("job", "unknown"))),
            message=annotations.get("description", annotations.get("summary", "")),
            labels=labels,
            annotations=annotations,
        )

    @classmethod
    def from_event_payload(cls, payload: dict[str, Any]) -> Alert:
        """Parse a K8s event watcher payload into an Alert."""
        return cls(
            alertname=payload.get("reason", "K8sEvent"),
            severity=AlertSeverity.WARNING,
            namespace=payload.get("namespace", "default"),
            source=f"{payload.get('resource_kind', 'pod')}/{payload.get('resource_name', 'unknown')}",
            message=payload.get("message", ""),
            labels={"source": "k8s_event"},
        )


# ── Action types ──────────────────────────────────────────────────────────────


@dataclass
class ActionStep:
    """A single step in an action plan."""
    tool: str                         # "kubectl", "helm", "terraform", "system"
    command: str                      # The actual command/operation
    args: dict[str, Any] = field(default_factory=dict)
    dry_run_safe: bool = True         # Can this be safely dry-run?
    description: str = ""             # Human-readable description of what this does


@dataclass
class ActionPlan:
    """
    An LLM-generated action plan — a sequence of steps to recover from an incident.

    The plan goes through dry-run validation before execution.
    """
    steps: list[ActionStep]
    rationale: str                    # Why the LLM thinks this will work
    estimated_risk: Literal["low", "medium", "high"] = "low"
    generated_by: str = "llm"        # "llm" | "tier0" | "runbook"


@dataclass
class ActionResult:
    """The result of executing one ActionStep."""
    step: ActionStep
    success: bool
    output: str = ""
    error: str = ""
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    dry_run: bool = False


@dataclass
class DryRunResult:
    """The result of dry-running an ActionPlan."""
    plan: ActionPlan
    all_passed: bool
    results: list[ActionResult] = field(default_factory=list)
    failure_reason: str = ""


@dataclass
class VerificationResult:
    """Did the executed plan actually fix the problem?"""
    success: bool
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    verified_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""


# ── The main state ────────────────────────────────────────────────────────────


from typing import TypedDict


class AgentState(TypedDict, total=False):
    """
    The complete state of the agent graph.

    Every node reads from this dict and returns a partial dict of updates.
    LangGraph merges the updates automatically.

    Keys are optional (total=False) because nodes only write what they produce.
    """

    # ── Mode & incident tracking ──────────────────────────────
    mode: AgentMode
    incident_id: str              # Unique ID for the current incident (uuid)

    # ── Incoming alert ────────────────────────────────────────
    current_alert: Alert
    raw_alert_payload: dict[str, Any]       # Original unprocessed payload (kept for audit)

    # ── Pre-processed log context ─────────────────────────────
    error_signatures: list[ErrorSignature]   # From the log pre-processor

    # ── Triage outputs ────────────────────────────────────────
    failure_signature: str        # Structured key: "deployment/nginx:CrashLoopBackOff"
    is_tier0: bool                # True if Tier-0 can handle this deterministically
    tier0_playbook_name: str      # Which Tier-0 playbook matched

    # ── Memory / runbook lookup ───────────────────────────────
    retrieved_runbook: dict[str, Any] | None    # The runbook from DynamoDB, if found
    runbook_confidence: float            # 0.0–1.0

    # ── LLM reasoning ────────────────────────────────────────
    diagnosis: str                # LLM's diagnosis of the root cause
    plan: ActionPlan              # The generated action plan

    # ── Execution ─────────────────────────────────────────────
    dry_run_result: DryRunResult
    actions_taken: list[ActionResult]
    verification_result: VerificationResult

    # ── Retries & outcome ─────────────────────────────────────
    retry_count: int
    max_retries: int
    outcome: IncidentOutcome

    # ── Cost & safety tracking ────────────────────────────────
    cost_usd: float               # Running total of Bedrock API cost
    token_count: int              # Running total of tokens used
    llm_calls: int                # Number of LLM calls made this incident

    # ── Human-readable incident log ───────────────────────────
    incident_log: list[str]       # Append-only log of what happened
