"""Integration test: Agent exceeds rate limit -> Watchdog intervenes and triggers kill switch."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from watchdog.config import WatchdogSettings
from watchdog.killswitch import DENY_ALL_POLICY_NAME, KillSwitch
from watchdog.rules import WatchdogRuleEngine
from watchdog.service import app, watchdog_service


@pytest.mark.asyncio
async def test_agent_exceeds_rate_limit_triggers_watchdog_killswitch() -> None:
    """Verify that exceeding rate limit triggers IAM revocation and K8s scale-down."""
    mock_iam = MagicMock()
    mock_sns = MagicMock()
    mock_http = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_http.patch.return_value = mock_resp

    # Configure max 3 incidents per hour for the test
    settings = WatchdogSettings(
        agent_iam_role_name="homeostat-agent",
        agent_deployment_name="homeostat-operator",
        agent_namespace="homeostat",
        sns_topic_arn="arn:aws:sns:us-east-1:123456789012:homeostat-alerts",
        max_incidents_per_hour=3,
        max_pod_deletions_per_incident=5,
    )
    rule_engine = WatchdogRuleEngine(
        spend_cap_usd=20.0,
        max_incidents_per_hour=3,
        max_pod_deletions_per_incident=5,
    )
    killswitch = KillSwitch(
        settings=settings,
        iam_client=mock_iam,
        sns_client=mock_sns,
        http_client=mock_http,
    )

    watchdog_service.settings = settings
    watchdog_service.rule_engine = rule_engine
    watchdog_service.killswitch = killswitch

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Initial health check: watchdog is healthy, kill switch is inactive
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["killswitch_active"] is False

        # First 3 incidents within the hour: should succeed
        for i in range(1, 4):
            resp = await client.post(
                "/incident_action",
                json={
                    "incident_id": f"incident-{i}",
                    "action": "kubectl rollout restart",
                    "target": "apps/v1/Deployment/shop/frontend",
                    "pod_deletions": 1,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "approved"
            assert resp.json()["actions_in_last_hour"] == i

        # Verify kill switch has NOT been triggered yet
        assert killswitch.is_activated is False
        mock_iam.put_role_policy.assert_not_called()
        mock_sns.publish.assert_not_called()

        # 4th incident attempt in the same hour: EXCEEDS LIMIT of 3
        intervened_resp = await client.post(
            "/incident_action",
            json={
                "incident_id": "incident-4",
                "action": "kubectl rollout restart",
                "target": "apps/v1/Deployment/shop/frontend",
                "pod_deletions": 1,
            },
        )
        # Should be rejected with 429 Too Many Requests
        assert intervened_resp.status_code == 429
        assert "Rate limit exceeded" in intervened_resp.json()["detail"]

        # VERIFY WATCHDOG INTERVENTION:
        # 1. Kill switch is activated
        assert killswitch.is_activated is True
        assert "Rate limit exceeded" in (killswitch.activation_reason or "")

        # 2. Agent IAM role permissions revoked with Deny-All policy
        mock_iam.put_role_policy.assert_called_once()
        iam_call_kwargs = mock_iam.put_role_policy.call_args.kwargs
        assert iam_call_kwargs["RoleName"] == "homeostat-agent"
        assert iam_call_kwargs["PolicyName"] == DENY_ALL_POLICY_NAME

        # 3. Agent K8s deployment scaled to 0
        mock_http.patch.assert_called_once()
        k8s_call_kwargs = mock_http.patch.call_args[1]
        assert k8s_call_kwargs["json"] == {"spec": {"replicas": 0}}

        # 4. Human alert sent via SNS
        mock_sns.publish.assert_called_once()
        sns_call_kwargs = mock_sns.publish.call_args.kwargs
        assert "KILL SWITCH ACTIVATED" in sns_call_kwargs["Subject"]
        assert "Rate limit exceeded" in sns_call_kwargs["Message"]

        # 5. Subsequent attempts by agent are blocked with 403 Forbidden
        subsequent_resp = await client.post(
            "/incident_action",
            json={
                "incident_id": "incident-5",
                "action": "kubectl get pods",
            },
        )
        assert subsequent_resp.status_code == 403
        assert "Kill switch is active" in subsequent_resp.json()["detail"]
