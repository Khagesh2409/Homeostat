"""Unit and integration tests for ShadowTool and the Maintenance node."""

import json
from unittest.mock import MagicMock

from homeostat.nodes.maintenance import maintenance
from homeostat.state import AgentMode, AgentState, Alert, AlertSeverity
from homeostat.tools.kubectl import KubectlTool
from homeostat.tools.shadow import ShadowTool


def test_shadow_tool_clone_and_clean() -> None:
    """Test ShadowTool cloning a deployment, stripping metadata, and cleanup."""
    mock_kubectl = MagicMock(spec=KubectlTool)
    mock_kubectl.get.return_value = {
        "success": True,
        "output": json.dumps({
            "metadata": {
                "name": "cart-service",
                "namespace": "shop",
                "resourceVersion": "9999",
                "uid": "abc-123",
            },
            "spec": {"replicas": 3},
            "status": {"availableReplicas": 3},
        }),
    }
    mock_kubectl.apply.return_value = {"success": True, "output": "applied"}
    mock_kubectl.run_command.return_value = {"success": True, "output": "deleted"}

    shadow = ShadowTool(kubectl=mock_kubectl)
    clone_res = shadow.clone_workload(
        source_workload="cart-service",
        source_namespace="shop",
        shadow_namespace="homeostat-shadow",
        scale=1,
    )

    assert clone_res["success"] is True
    assert clone_res["shadow_workload"] == "cart-service"
    # Check stripped metadata
    cloned = clone_res["cloned_manifest"]
    assert cloned["metadata"]["namespace"] == "homeostat-shadow"
    assert "resourceVersion" not in cloned["metadata"]
    assert "uid" not in cloned["metadata"]
    assert cloned["metadata"]["labels"]["homeostat.io/environment"] == "shadow"
    assert cloned["spec"]["replicas"] == 1
    assert "status" not in cloned

    # Test cleanup
    del_res = shadow.cleanup_shadow(workload="cart-service")
    assert del_res["success"] is True
    mock_kubectl.run_command.assert_called_once_with(
        "delete",
        {
            "resource": "deployment",
            "name": "cart-service",
            "namespace": "homeostat-shadow",
        },
        dry_run=False,
    )


def test_maintenance_cycle_successful() -> None:
    """Test full maintenance cycle where candidate change passes shadow and applies to prod."""
    mock_shadow = MagicMock(spec=ShadowTool)
    mock_kubectl = MagicMock(spec=KubectlTool)

    mock_shadow.clone_workload.return_value = {"success": True}
    mock_shadow.apply_candidate_change.return_value = {"success": True}
    mock_shadow.check_shadow_health.return_value = {"success": True, "healthy": True}
    mock_shadow.cleanup_shadow.return_value = {"success": True}

    mock_kubectl.apply.return_value = {"success": True, "output": "configured"}

    state: AgentState = {
        "mode": AgentMode.IDLE,
        "incident_log": [],
        "maintenance_candidates": [
            {
                "type": "resource_limit_tuning",
                "workload": "payment-api",
                "source_namespace": "production",
                "patch": {"spec": {"replicas": 2}},
            }
        ],
    }

    result = maintenance(state, shadow=mock_shadow, kubectl=mock_kubectl)

    assert result["maintenance_outcome"] == "completed"
    assert len(result["maintenance_applied"]) == 1
    assert len(result["maintenance_rejected"]) == 0

    # Verify rehearsal before production apply
    mock_shadow.clone_workload.assert_called_once_with(
        source_workload="payment-api",
        source_namespace="production",
        shadow_namespace="homeostat-shadow",
    )
    mock_shadow.apply_candidate_change.assert_called_once()
    mock_shadow.check_shadow_health.assert_called_once()

    # Verify production apply called after successful shadow test
    mock_kubectl.apply.assert_called_once_with(
        content=json.dumps({"spec": {"replicas": 2}}),
        namespace="production",
    )
    mock_shadow.cleanup_shadow.assert_called_once_with(
        workload="payment-api",
        namespace="homeostat-shadow",
    )


def test_maintenance_cycle_discards_failed_shadow_change() -> None:
    """Test that a candidate failing shadow health check is discarded and prod is untouched."""
    mock_shadow = MagicMock(spec=ShadowTool)
    mock_kubectl = MagicMock(spec=KubectlTool)

    mock_shadow.clone_workload.return_value = {"success": True}
    mock_shadow.apply_candidate_change.return_value = {"success": True}
    # Shadow check fails (e.g. crash loop or bad config)
    mock_shadow.check_shadow_health.return_value = {
        "success": False,
        "healthy": False,
        "error": "Pod CrashLoopBackOff in shadow",
    }
    mock_shadow.cleanup_shadow.return_value = {"success": True}

    state: AgentState = {
        "mode": AgentMode.IDLE,
        "incident_log": [],
        "maintenance_candidates": [
            {
                "type": "bad_config_experiment",
                "workload": "broken-service",
                "source_namespace": "production",
                "patch": {"spec": {"broken": True}},
            }
        ],
    }

    result = maintenance(state, shadow=mock_shadow, kubectl=mock_kubectl)

    assert result["maintenance_outcome"] == "discarded"
    assert len(result["maintenance_applied"]) == 0
    assert len(result["maintenance_rejected"]) == 1
    assert "CrashLoopBackOff" in result["maintenance_rejected"][0]["reason"]

    # CRITICAL INVARIANT: Production kubectl.apply must NEVER be called
    mock_kubectl.apply.assert_not_called()
    # Shadow must still be cleaned up
    mock_shadow.cleanup_shadow.assert_called_once_with(
        workload="broken-service",
        namespace="homeostat-shadow",
    )


def test_maintenance_interrupted_by_active_alert() -> None:
    """Test that active incoming alerts immediately halt maintenance."""
    mock_shadow = MagicMock(spec=ShadowTool)
    mock_kubectl = MagicMock(spec=KubectlTool)

    alert = Alert(
        alertname="KubePodCrashLooping",
        severity=AlertSeverity.CRITICAL,
        namespace="shop",
        source="pod/frontend-xyz",
        message="Pod is crash looping",
    )

    state: AgentState = {
        "mode": AgentMode.INCIDENT,
        "current_alert": alert,
        "incident_log": [],
        "maintenance_candidates": [
            {
                "type": "version_bump",
                "workload": "frontend",
                "source_namespace": "shop",
            }
        ],
    }

    result = maintenance(state, shadow=mock_shadow, kubectl=mock_kubectl)

    assert result["maintenance_outcome"] == "interrupted_by_alert"
    mock_shadow.clone_workload.assert_not_called()
    mock_kubectl.apply.assert_not_called()
