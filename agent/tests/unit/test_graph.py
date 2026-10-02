"""
Tests for the LangGraph agent state machine routing and node execution.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from langgraph.graph import StateGraph

from homeostat.graph import build_graph
from homeostat.state import AgentMode, AgentState, Alert, AlertSeverity, IncidentOutcome


def test_triage_tier0_routing():
    from homeostat.nodes.triage import triage, route_after_triage
    
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


def test_triage_memory_lookup_routing():
    from homeostat.nodes.triage import triage, route_after_triage
    
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


def test_memory_lookup_routing():
    from homeostat.nodes.memory_lookup import route_after_memory_lookup
    
    # Confidence above threshold -> validate
    state: AgentState = {"retrieved_runbook": {"dummy": "data"}, "runbook_confidence": 0.8}
    assert route_after_memory_lookup(state) == "validate"
    
    # Confidence below threshold -> diagnose
    state: AgentState = {"retrieved_runbook": {"dummy": "data"}, "runbook_confidence": 0.5}
    assert route_after_memory_lookup(state) == "diagnose"
    
    # No runbook -> diagnose
    state: AgentState = {"retrieved_runbook": None, "runbook_confidence": 0.0}
    assert route_after_memory_lookup(state) == "diagnose"


def test_validate_routing():
    from homeostat.nodes.validate import validate, route_after_validate
    
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
def test_diagnose_node(mock_boto_client):
    from homeostat.nodes.diagnose import diagnose
    
    mock_bedrock = MagicMock()
    mock_boto_client.return_value = mock_bedrock
    
    mock_response = {
        "body": MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": '{"root_cause": "OOM due to memory leak", "confidence": 0.9}'}],
                "usage": {"input_tokens": 100, "output_tokens": 50}
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
