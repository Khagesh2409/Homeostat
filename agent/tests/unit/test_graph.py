"""
Tests for the LangGraph agent state machine routing and node execution.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from homeostat.state import (
    ActionPlan,
    ActionResult,
    ActionStep,
    AgentMode,
    AgentState,
    Alert,
    AlertSeverity,
)


def test_triage_tier0_routing() -> None:
    from homeostat.nodes.triage import route_after_triage, triage

    alert = Alert(
        alertname="KubePodCrashLooping",
        severity=AlertSeverity.CRITICAL,
        namespace="default",
        source="pod/nginx",
        message="crash",
    )

    state: AgentState = {"current_alert": alert}
    result = triage(state)

    assert result["is_tier0"] is True
    assert result["mode"] == AgentMode.INCIDENT

    # State update
    state.update(result)

    route = route_after_triage(state)
    assert route == "tier0"


def test_triage_memory_lookup_routing() -> None:
    from homeostat.nodes.triage import route_after_triage, triage

    alert = Alert(
        alertname="SomeUnknownAlert",
        severity=AlertSeverity.WARNING,
        namespace="default",
        source="pod/nginx",
        message="unknown",
    )

    state: AgentState = {"current_alert": alert}
    result = triage(state)

    assert result["is_tier0"] is False

    state.update(result)
    route = route_after_triage(state)
    assert route == "memory_lookup"


def test_memory_lookup_routing() -> None:
    from homeostat.nodes.memory_lookup import route_after_memory_lookup

    # Confidence above threshold -> validate
    state: AgentState = {"retrieved_runbook": {"dummy": "data"}, "runbook_confidence": 0.8}
    assert route_after_memory_lookup(state) == "validate"

    # Confidence below threshold -> diagnose
    state2: AgentState = {"retrieved_runbook": {"dummy": "data"}, "runbook_confidence": 0.5}
    assert route_after_memory_lookup(state2) == "diagnose"

    # No runbook -> diagnose
    state3: AgentState = {"retrieved_runbook": None, "runbook_confidence": 0.0}
    assert route_after_memory_lookup(state3) == "diagnose"


def test_validate_routing() -> None:
    from homeostat.nodes.validate import route_after_validate, validate

    alert = Alert(
        alertname="CrashLoopBackOff",
        severity=AlertSeverity.WARNING,
        namespace="default",
        source="deployment/nginx",
        message="",
    )

    # Matching runbook
    runbook = {"failure_signature": "deployment/nginx:CrashLoopBackOff"}
    state: AgentState = {
        "current_alert": alert,
        "failure_signature": "deployment/nginx:CrashLoopBackOff",
        "retrieved_runbook": runbook,
    }

    result = validate(state)
    assert "retrieved_runbook" not in result # it keeps it as is
    state.update(result)
    assert route_after_validate(state) == "execute"

    # Mismatching runbook (different resource type)
    runbook_mismatch = {"failure_signature": "node/*:CrashLoopBackOff"}
    state_mismatch: AgentState = {
        "current_alert": alert,
        "failure_signature": "deployment/nginx:CrashLoopBackOff",
        "retrieved_runbook": runbook_mismatch,
    }

    result_mismatch = validate(state_mismatch)
    assert result_mismatch["retrieved_runbook"] is None
    state_mismatch.update(result_mismatch)
    assert route_after_validate(state_mismatch) == "diagnose"


@patch("homeostat.llm.bedrock.boto3.client")
def test_diagnose_node(mock_boto_client: MagicMock) -> None:
    from homeostat.nodes.diagnose import diagnose

    mock_bedrock = MagicMock()
    mock_boto_client.return_value = mock_bedrock

    mock_response = {
        "body": MagicMock(
            read=lambda: json.dumps({
                "content": [{
                    "type": "text",
                    "text": '{"root_cause": "OOM due to memory leak", "confidence": 0.9}',
                }],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }).encode("utf-8")
        )
    }
    mock_bedrock.invoke_model.return_value = mock_response

    alert = Alert(
        alertname="OOMKilled",
        severity=AlertSeverity.CRITICAL,
        namespace="default",
        source="pod/db",
        message="",
    )

    state: AgentState = {"current_alert": alert}
    result = diagnose(state)

    assert "OOM due to memory leak" in result["diagnosis"]
    assert result["llm_calls"] == 1
    assert result["token_count"] == 150


@patch("homeostat.tools.executor.execute_step")
def test_dry_run_success(mock_exec: MagicMock) -> None:
    from homeostat.nodes.dry_run import dry_run, route_after_dry_run

    step = ActionStep(
        tool="kubectl", command="delete_pod", args={"name": "nginx"}, dry_run_safe=True
    )
    mock_exec.return_value = ActionResult(step=step, success=True, dry_run=True)
    plan = ActionPlan(steps=[step], rationale="test", generated_by="llm")
    state: AgentState = {"plan": plan, "retry_count": 0}

    result = dry_run(state)
    assert result["dry_run_result"].all_passed is True

    state.update(result)
    assert route_after_dry_run(state) == "execute"


@patch("homeostat.tools.executor.execute_step")
def test_dry_run_llm_failure_routes_to_diagnose_then_escalate(mock_exec: MagicMock) -> None:
    from homeostat.nodes.dry_run import dry_run, route_after_dry_run

    step = ActionStep(tool="kubectl", command="delete pod nginx", dry_run_safe=True)
    mock_exec.return_value = ActionResult(step=step, success=False, error="Dry run rejected")

    plan = ActionPlan(steps=[step], rationale="test", generated_by="llm")
    state: AgentState = {"plan": plan, "retry_count": 0, "max_retries": 2}

    # Attempt 1
    result1 = dry_run(state)
    assert result1["retry_count"] == 1
    state.update(result1)
    assert route_after_dry_run(state) == "diagnose"

    # Attempt 2 -> hits max_retries
    result2 = dry_run(state)
    assert result2["retry_count"] == 2
    state.update(result2)
    assert route_after_dry_run(state) == "escalate"


@patch("homeostat.tools.executor.execute_step")
def test_dry_run_tier0_failure_records_miss(mock_exec: MagicMock) -> None:
    from homeostat.nodes.dry_run import dry_run, route_after_dry_run
    from homeostat.tier0.cooldown import default_tracker

    default_tracker.reset()
    step = ActionStep(tool="kubectl", command="delete pod nginx", dry_run_safe=True)
    mock_exec.return_value = ActionResult(step=step, success=False, error="Simulated failure")

    plan = ActionPlan(steps=[step], rationale="test", generated_by="tier0")
    state: AgentState = {
        "plan": plan,
        "tier0_playbook_name": "pod-restart",
        "tier0_target": "default/workload/nginx",
        "retry_count": 0,
    }

    result = dry_run(state)
    assert result["tier0_miss"] is True
    assert result["tier0_playbook_name"] == ""
    assert "retry_count" not in result  # Doesn't burn LLM retry count

    state.update(result)
    assert route_after_dry_run(state) == "diagnose"
    assert default_tracker.misses("pod-restart:default/workload/nginx") == 1

