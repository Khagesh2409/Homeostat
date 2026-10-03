"""Unit tests for the Watchdog Kill Switch."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from watchdog.config import WatchdogSettings
from watchdog.killswitch import DENY_ALL_POLICY_NAME, KillSwitch


@pytest.fixture
def mock_settings() -> WatchdogSettings:
    """Return test settings for watchdog."""
    return WatchdogSettings(
        agent_iam_role_name="test-homeostat-agent",
        agent_deployment_name="test-homeostat-operator",
        agent_namespace="test-homeostat",
        sns_topic_arn="arn:aws:sns:us-east-1:123456789012:test-topic",
    )


@pytest.mark.asyncio
async def test_killswitch_activate_success(mock_settings: WatchdogSettings) -> None:
    """Test successful kill switch activation across IAM, K8s, and SNS."""
    mock_iam = MagicMock()
    mock_sns = MagicMock()
    mock_http = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_http.patch.return_value = mock_resp

    ks = KillSwitch(
        settings=mock_settings,
        iam_client=mock_iam,
        sns_client=mock_sns,
        http_client=mock_http,
    )

    record = await ks.activate(
        reason="Spend cap exceeded",
        trigger_source="SpendCapRule",
    )

    assert ks.is_activated is True
    assert ks.activation_reason == "Spend cap exceeded"
    assert record.action == "ACTIVATE"
    assert record.iam_revoked is True
    assert record.k8s_scaled is True
    assert record.sns_alert_sent is True

    # Verify IAM call
    mock_iam.put_role_policy.assert_called_once()
    call_kwargs = mock_iam.put_role_policy.call_args.kwargs
    assert call_kwargs["RoleName"] == "test-homeostat-agent"
    assert call_kwargs["PolicyName"] == DENY_ALL_POLICY_NAME
    assert "Deny" in call_kwargs["PolicyDocument"]

    # Verify K8s call
    mock_http.patch.assert_called_once()
    assert mock_http.patch.call_args[1]["json"] == {"spec": {"replicas": 0}}

    # Verify SNS call
    mock_sns.publish.assert_called_once()
    assert "KILL SWITCH ACTIVATED" in mock_sns.publish.call_args.kwargs["Subject"]


@pytest.mark.asyncio
async def test_killswitch_restore_success(mock_settings: WatchdogSettings) -> None:
    """Test restoring the agent after a kill switch event."""
    mock_iam = MagicMock()
    mock_sns = MagicMock()
    mock_http = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_http.patch.return_value = mock_resp

    ks = KillSwitch(
        settings=mock_settings,
        iam_client=mock_iam,
        sns_client=mock_sns,
        http_client=mock_http,
    )
    ks.is_activated = True
    ks.activation_reason = "Manual shutdown"

    record = await ks.restore(reason="Admin resolved issue")

    assert ks.is_activated is False
    assert ks.activation_reason is None
    assert record.action == "RESTORE"
    assert record.iam_revoked is True
    assert record.k8s_scaled is True
    assert record.sns_alert_sent is True

    # Verify IAM deny policy deleted
    mock_iam.delete_role_policy.assert_called_once_with(
        RoleName="test-homeostat-agent",
        PolicyName=DENY_ALL_POLICY_NAME,
    )

    # Verify K8s scaled back to 1
    assert mock_http.patch.call_args[1]["json"] == {"spec": {"replicas": 1}}

    # Verify SNS restoration notification
    assert "AGENT RESTORED" in mock_sns.publish.call_args.kwargs["Subject"]


@pytest.mark.asyncio
async def test_killswitch_handles_partial_failures(mock_settings: WatchdogSettings) -> None:
    """Test that kill switch continues attempting other actions if IAM fails."""
    mock_iam = MagicMock()
    mock_iam.put_role_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
        "PutRolePolicy",
    )
    mock_sns = MagicMock()
    mock_http = AsyncMock()
    mock_http.patch.side_effect = RuntimeError("Connection refused")

    ks = KillSwitch(
        settings=mock_settings,
        iam_client=mock_iam,
        sns_client=mock_sns,
        http_client=mock_http,
    )

    record = await ks.activate(reason="Test partial failure")

    assert ks.is_activated is True
    assert record.iam_revoked is False
    assert record.k8s_scaled is False
    # SNS alert should still be sent
    assert record.sns_alert_sent is True
    mock_sns.publish.assert_called_once()
