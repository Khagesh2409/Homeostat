"""Scenario definitions and result models for the chaos engineering framework."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class ChaosScenario:
    """Definition of a reproducible fault injection scenario."""

    name: str
    category: str  # "pod", "config", "disk", "network", "node", "injection"
    description: str
    attack: Callable[[], Any]
    setup: Callable[[], Any] | None = None
    expected_detection_max_s: float = 60.0
    expected_recovery_max_s: float = 300.0
    safety_invariants: list[str] = field(default_factory=list)
    cleanup: Callable[[], Any] | None = None
    verify_recovered: Callable[[], bool] | None = None


@dataclass
class ChaosResult:
    """Structured record of the outcome and metrics from a chaos scenario run."""

    scenario: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    detection_time_s: float = 0.0
    diagnosis_time_s: float = 0.0
    recovery_time_s: float = 0.0
    total_time_s: float = 0.0
    cost_usd: float = 0.0
    tier_used: str = "tier0"  # "tier0", "runbook", "llm_fresh"
    safety_violations: list[str] = field(default_factory=list)
    runbook_used: bool = False
    runbook_was_correct: bool = False
    success: bool = False
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert result to JSON-serializable dictionary."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChaosResult:
        """Construct ChaosResult from a dictionary."""
        payload = dict(data)
        if isinstance(payload.get("timestamp"), str):
            payload["timestamp"] = datetime.fromisoformat(payload["timestamp"])
        return cls(**payload)
