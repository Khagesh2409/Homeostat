"""Unit tests for the Watchdog FastAPI service and monitoring loop."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from watchdog.config import WatchdogSettings
from watchdog.killswitch import KillSwitch
from watchdog.rules import WatchdogRuleEngine
from watchdog.service import app, watchdog_service


@pytest.fixture
def mock_service_deps() -> tuple[MagicMock, MagicMock, AsyncMock]:
    """Provide mocked dependencies for WatchdogService."""
    mock_iam = MagicMock()
    mock_sns = MagicMock()
    mock_ce = MagicMock()
    mock_http = AsyncMock()

    settings = WatchdogSettings(
        agent_iam_role_name="test-agent",
        max_incidents_per_hour=3,
        max_pod_deletions_per_incident=2,
    )
    rule_engine = WatchdogRuleEngine(
        spend_cap_usd=20.0,
        max_incidents_per_hour=3,
        max_pod_deletions_per_incident=2,
    )
    ks = KillSwitch(
        settings=settings,
        iam_client=mock_iam,
        sns_client=mock_sns,
        http_client=mock_http,
    )

    # Re-wire global watchdog_service for tests
    watchdog_service.settings = settings
    watchdog_service.rule_engine = rule_engine
    watchdog_service.killswitch = ks
    watchdog_service._ce_client = mock_ce

    return mock_iam, mock_sns, mock_ce


@pytest.mark.asyncio
async def test_health_and_status_endpoints(
    mock_service_deps: tuple[MagicMock, MagicMock, AsyncMock],
) -> None:
    """Test /health and /status endpoints return valid metrics."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data["status"] == "healthy"
        assert health_data["killswitch_active"] is False

        status_resp = await client.get("/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["killswitch_active"] is False
        assert status_data["max_incidents_per_hour"] == 3


@pytest.mark.asyncio
async def test_heartbeat_endpoint(
    mock_service_deps: tuple[MagicMock, MagicMock, AsyncMock],
) -> None:
    """Test receiving agent heartbeat."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/heartbeat",
            json={"agent_id": "test-agent-pod-1", "active_incidents": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "acknowledged"
        assert data["killswitch_active"] is False
        assert watchdog_service.rule_engine.heartbeat_rule.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_verify_action_endpoint(
    mock_service_deps: tuple[MagicMock, MagicMock, AsyncMock],
) -> None:
    """Test pre-flight verification of proposed actions."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Allowed safe action
        safe_resp = await client.post(
            "/verify_action",
            json={
                "incident_id": "inc-1",
                "action": "kubectl rollout restart deployment",
                "target": "apps/v1/Deployment/shop/checkout",
                "pod_deletions": 1,
            },
        )
        assert safe_resp.status_code == 200
        assert safe_resp.json()["allowed"] is True

        # 2. Blocked namespace deletion
        bad_ns_resp = await client.post(
            "/verify_action",
            json={
                "incident_id": "inc-2",
                "action": "kubectl delete namespace",
                "target": "namespaces/kube-system",
                "namespace": "kube-system",
            },
        )
        assert bad_ns_resp.status_code == 200
        assert bad_ns_resp.json()["allowed"] is False

        # 3. Blocked watchdog modification
        bad_role_resp = await client.post(
            "/verify_action",
            json={
                "incident_id": "inc-3",
                "action": "iam:PutRolePolicy",
                "target": "arn:aws:iam::123:role/homeostat-watchdog",
            },
        )
        assert bad_role_resp.status_code == 200
        assert bad_role_resp.json()["allowed"] is False


@pytest.mark.asyncio
async def test_manual_kill_and_restore_endpoints(
    mock_service_deps: tuple[MagicMock, MagicMock, AsyncMock],
) -> None:
    """Test manual kill switch triggering and restoration."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Trigger kill switch
        kill_resp = await client.post(
            "/kill",
            json={"reason": "Operator manual drill"},
        )
        assert kill_resp.status_code == 200
        assert kill_resp.json()["status"] == "activated"
        assert watchdog_service.killswitch.is_activated is True

        # When kill switch is active, incident actions must be blocked (403)
        act_resp = await client.post(
            "/incident_action",
            json={"incident_id": "inc-blocked", "action": "restart"},
        )
        assert act_resp.status_code == 403

        # Restore
        restore_resp = await client.post(
            "/restore",
            json={"reason": "Drill completed successfully"},
        )
        assert restore_resp.status_code == 200
        assert restore_resp.json()["status"] == "restored"
        assert watchdog_service.killswitch.is_activated is False
