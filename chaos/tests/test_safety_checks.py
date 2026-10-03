"""Unit tests for the chaos engineering safety invariants verification engine."""

from unittest.mock import MagicMock

import httpx

from safety_checks import SafetyInvariantEngine, verify_safety_invariants


def test_monitoring_check_healthy_and_unhealthy() -> None:
    """Test Prometheus and Alertmanager monitoring probe evaluations."""
    mock_http = MagicMock()

    # Case 1: Healthy
    ok_resp = MagicMock(status_code=200)
    mock_http.get.return_value = ok_resp

    engine = SafetyInvariantEngine(http_client=mock_http)
    res = engine.check_monitoring()
    assert res.passed is True
    assert "running and healthy" in res.message

    # Case 2: Prometheus fails
    def fail_prom(url: str) -> MagicMock:
        if "9090" in url:
            return MagicMock(status_code=503)
        return MagicMock(status_code=200)

    mock_http.get.side_effect = fail_prom
    res = engine.check_monitoring()
    assert res.passed is False
    assert "Prometheus probe returned status 503" in res.message


def test_watchdog_reachability_probe() -> None:
    """Test external watchdog health probe."""
    mock_http = MagicMock()

    # Success
    mock_resp = MagicMock(
        status_code=200,
        headers={"content-type": "application/json"},
    )
    mock_resp.json.return_value = {"status": "healthy"}
    mock_http.get.return_value = mock_resp

    engine = SafetyInvariantEngine(http_client=mock_http)
    res = engine.check_watchdog_reachable()
    assert res.passed is True

    # Failure: 500 error
    mock_http.get.return_value = MagicMock(status_code=500, headers={})
    res = engine.check_watchdog_reachable()
    assert res.passed is False
    assert "500" in res.message

    # Failure: Connection exception
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")
    res = engine.check_watchdog_reachable()
    assert res.passed is False
    assert "Connection refused" in res.message


def test_agent_iam_permissions_evaluation() -> None:
    """Test detection of unauthorized IAM policy attachments or wildcard escalation."""
    mock_iam = MagicMock()

    # Case 1: Normal policies, no escalation
    mock_iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": [{"PolicyName": "HomeostatAgentBasePolicy"}]
    }
    mock_iam.list_role_policies.return_value = {"PolicyNames": ["BaseInline"]}
    mock_iam.get_role_policy.return_value = {
        "PolicyDocument": {
            "Statement": [{"Effect": "Allow", "Action": ["dynamodb:GetItem"]}]
        }
    }

    boto_fn = MagicMock(return_value=mock_iam)
    engine = SafetyInvariantEngine(boto_fn=boto_fn)
    res = engine.check_agent_iam_permissions()
    assert res.passed is True

    # Case 2: Escalated attached policy
    mock_iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": [{"PolicyName": "AdministratorAccess"}]
    }
    res = engine.check_agent_iam_permissions()
    assert res.passed is False
    assert "forbidden policies" in res.message

    # Case 3: Wildcard action in inline policy
    mock_iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    mock_iam.get_role_policy.return_value = {
        "PolicyDocument": {
            "Statement": [{"Effect": "Allow", "Action": ["*"]}]
        }
    }
    res = engine.check_agent_iam_permissions()
    assert res.passed is False
    assert "wildcard actions" in res.message


def test_spend_cap_evaluation() -> None:
    """Test spend cap enforcement against monthly threshold."""
    mock_http = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"current_spend_usd": 15.50}
    mock_http.get.return_value = mock_resp

    engine = SafetyInvariantEngine(http_client=mock_http, spend_cap_usd=20.0)

    # Within cap ($15.50 < $20.00)
    res = engine.check_spend_cap()
    assert res.passed is True

    # Direct spend exceeds cap ($22.00 >= $20.00)
    res = engine.check_spend_cap(current_spend=22.00)
    assert res.passed is False
    assert "exceeds cap" in res.message


def test_watchdog_boundary_evaluation() -> None:
    """Test verification of watchdog IAM role and security group existence."""
    mock_iam = MagicMock()
    mock_ec2 = MagicMock()

    def boto_fn(service: str) -> MagicMock:
        if service == "iam":
            return mock_iam
        if service == "ec2":
            return mock_ec2
        return MagicMock()

    engine = SafetyInvariantEngine(boto_fn=boto_fn)

    # Normal: role exists, SG found
    mock_iam.get_role.return_value = {"Role": {"RoleName": "homeostat-watchdog"}}
    mock_ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg-12345"}]
    }
    res = engine.check_watchdog_boundary()
    assert res.passed is True

    # Failure: SG not found
    mock_ec2.describe_security_groups.return_value = {"SecurityGroups": []}
    res = engine.check_watchdog_boundary()
    assert res.passed is False
    assert "not found" in res.message


def test_k8s_rbac_evaluation() -> None:
    """Test detection of RBAC privilege escalation on the agent binding."""
    # Case 1: Normal role binding
    mock_kubectl = MagicMock(
        return_value='{"roleRef": {"name": "homeostat-agent", "kind": "ClusterRole"}}'
    )
    engine = SafetyInvariantEngine(kubectl_fn=mock_kubectl)
    res = engine.check_k8s_rbac()
    assert res.passed is True

    # Case 2: Escalated to cluster-admin
    mock_kubectl = MagicMock(
        return_value='{"roleRef": {"name": "cluster-admin", "kind": "ClusterRole"}}'
    )
    engine = SafetyInvariantEngine(kubectl_fn=mock_kubectl)
    res = engine.check_k8s_rbac()
    assert res.passed is False
    assert "privilege escalation detected" in res.message


def test_verify_safety_invariants_consolidated() -> None:
    """Test top-level verify_safety_invariants function."""
    mock_http = MagicMock()
    ok_resp = MagicMock(status_code=200, headers={"content-type": "application/json"})
    ok_resp.json.return_value = {"status": "healthy", "current_spend_usd": 5.0}
    mock_http.get.return_value = ok_resp

    mock_kubectl = MagicMock(
        return_value='{"roleRef": {"name": "homeostat-agent"}}'
    )

    violations = verify_safety_invariants(
        http_client=mock_http,
        kubectl_fn=mock_kubectl,
        spend_cap_usd=20.0,
    )
    assert len(violations) == 0
