"""Kill switch mechanism for revoking agent IAM access and terminating execution."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
import structlog
from botocore.exceptions import ClientError

from watchdog.config import WatchdogSettings, get_settings

logger = structlog.get_logger(__name__)

DENY_ALL_POLICY_NAME = "HomeostatKillSwitchDenyAll"
DENY_ALL_POLICY_DOCUMENT = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WatchdogKillSwitchDenyAll",
                "Effect": "Deny",
                "Action": "*",
                "Resource": "*",
            }
        ],
    }
)


@dataclass
class KillSwitchRecord:
    """Record of a kill switch activation or restoration event."""

    action: str  # "ACTIVATE" or "RESTORE"
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    iam_revoked: bool = False
    k8s_scaled: bool = False
    sns_alert_sent: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class KillSwitch:
    """External emergency kill switch for the Homeostat agent."""

    def __init__(
        self,
        settings: WatchdogSettings | None = None,
        iam_client: Any | None = None,
        sns_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._iam = iam_client or boto3.client(
            "iam",
            region_name=self.settings.aws_region,
        )
        self._sns = sns_client or boto3.client(
            "sns",
            region_name=self.settings.aws_region,
        )
        self._http = http_client
        self.is_activated: bool = False
        self.activation_reason: str | None = None
        self.activated_at: datetime | None = None
        self.history: list[KillSwitchRecord] = []

    async def activate(
        self,
        reason: str,
        trigger_source: str = "watchdog_safety_rule",
    ) -> KillSwitchRecord:
        """Trigger emergency shutdown: deny IAM access, scale agent to 0, alert human."""
        logger.critical(
            "watchdog_killswitch_triggered",
            reason=reason,
            source=trigger_source,
        )

        record = KillSwitchRecord(
            action="ACTIVATE",
            reason=reason,
            timestamp=datetime.now(UTC),
            details={"trigger_source": trigger_source},
        )

        # 1. Revoke IAM permissions by putting Deny-All policy on agent role
        try:
            self._iam.put_role_policy(
                RoleName=self.settings.agent_iam_role_name,
                PolicyName=DENY_ALL_POLICY_NAME,
                PolicyDocument=DENY_ALL_POLICY_DOCUMENT,
            )
            record.iam_revoked = True
            logger.info(
                "agent_iam_revoked",
                role=self.settings.agent_iam_role_name,
            )
        except ClientError as e:
            logger.error("agent_iam_revoke_failed", error=str(e))
            record.details["iam_error"] = str(e)

        # 2. Scale Kubernetes deployment to 0 replicas
        scaled = await self._scale_agent_deployment(replicas=0)
        record.k8s_scaled = scaled

        # 3. Publish alert to SNS topic
        notified = self._send_sns_alert(
            subject=f"[HOMEOSTAT WATCHDOG] KILL SWITCH ACTIVATED: {reason[:60]}",
            message=(
                f"EMERGENCY KILL SWITCH ACTIVATED\n\n"
                f"Reason: {reason}\n"
                f"Trigger Source: {trigger_source}\n"
                f"Timestamp: {record.timestamp.isoformat()}\n"
                f"Agent IAM Role '{self.settings.agent_iam_role_name}' Deny-All policy applied.\n"
                f"Agent K8s Deployment '{self.settings.agent_deployment_name}' scaled to 0.\n"
            ),
        )
        record.sns_alert_sent = notified

        self.is_activated = True
        self.activation_reason = reason
        self.activated_at = record.timestamp
        self.history.append(record)

        return record

    async def restore(
        self,
        reason: str = "Admin manual restoration",
    ) -> KillSwitchRecord:
        """Restore normal operation: remove Deny-All policy and scale agent back up."""
        logger.warning(
            "watchdog_killswitch_restore_initiated",
            reason=reason,
        )

        record = KillSwitchRecord(
            action="RESTORE",
            reason=reason,
            timestamp=datetime.now(UTC),
        )

        # 1. Remove Deny-All policy from IAM role
        try:
            self._iam.delete_role_policy(
                RoleName=self.settings.agent_iam_role_name,
                PolicyName=DENY_ALL_POLICY_NAME,
            )
            record.iam_revoked = True  # Cleaned up deny policy
            logger.info(
                "agent_iam_deny_policy_removed",
                role=self.settings.agent_iam_role_name,
            )
        except ClientError as e:
            # If policy doesn't exist, treat as cleaned up
            if e.response.get("Error", {}).get("Code") == "NoSuchEntity":
                record.iam_revoked = True
            else:
                logger.error("agent_iam_restore_failed", error=str(e))
                record.details["iam_error"] = str(e)

        # 2. Scale Kubernetes deployment back to 1 replica
        scaled = await self._scale_agent_deployment(replicas=1)
        record.k8s_scaled = scaled

        # 3. Publish restoration notification to SNS
        notified = self._send_sns_alert(
            subject=f"[HOMEOSTAT WATCHDOG] AGENT RESTORED: {reason[:60]}",
            message=(
                f"AGENT RESTORATION NOTIFICATION\n\n"
                f"Reason: {reason}\n"
                f"Timestamp: {record.timestamp.isoformat()}\n"
                f"Agent IAM Role '{self.settings.agent_iam_role_name}' Deny-All policy removed.\n"
                f"Agent K8s Deployment '{self.settings.agent_deployment_name}' scaled to 1.\n"
            ),
        )
        record.sns_alert_sent = notified

        self.is_activated = False
        self.activation_reason = None
        self.activated_at = None
        self.history.append(record)

        return record

    async def _scale_agent_deployment(self, replicas: int) -> bool:
        """Scale agent Kubernetes deployment via Kubernetes API or HTTP client."""
        url = (
            f"{self.settings.k8s_api_url}/apis/apps/v1/namespaces/"
            f"{self.settings.agent_namespace}/deployments/"
            f"{self.settings.agent_deployment_name}/scale"
        )
        payload = {"spec": {"replicas": replicas}}
        headers = {"Content-Type": "application/merge-patch+json"}

        client = self._http or httpx.AsyncClient(timeout=5.0, verify=False)
        try:
            resp = await client.patch(url, json=payload, headers=headers)
            if resp.is_success:
                logger.info(
                    "agent_deployment_scaled",
                    replicas=replicas,
                    status=resp.status_code,
                )
                return True
            logger.warning(
                "agent_deployment_scale_failed",
                replicas=replicas,
                status=resp.status_code,
            )
            return False
        except (httpx.HTTPError, OSError, RuntimeError) as e:
            logger.warning(
                "agent_deployment_scale_exception",
                replicas=replicas,
                error=str(e),
            )
            return False
        finally:
            if self._http is None:
                await client.aclose()

    def _send_sns_alert(self, subject: str, message: str) -> bool:
        """Publish alert message to the configured SNS topic."""
        if not self.settings.sns_topic_arn:
            logger.warning("sns_topic_arn_not_configured_alert_skipped")
            return False

        try:
            self._sns.publish(
                TopicArn=self.settings.sns_topic_arn,
                Subject=subject[:100],
                Message=message,
            )
            logger.info("sns_alert_published", subject=subject[:50])
            return True
        except ClientError as e:
            logger.error("sns_publish_failed", error=str(e))
            return False
