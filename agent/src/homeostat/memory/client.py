"""
DynamoDB client for runbook storage.

Read/write runbooks to DynamoDB. Works against both real AWS and LocalStack.
The endpoint_url is None in prod (real AWS) and set to LocalStack URL in dev.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError

from homeostat.config import settings
from homeostat.memory.schemas import ActionStep, FailureSignature, Runbook

logger = logging.getLogger(__name__)


def _extract_sig_key(signature: FailureSignature | str) -> str:
    """Helper to get string partition key from FailureSignature or str."""
    if isinstance(signature, FailureSignature):
        return signature.key
    return str(signature)


class RunbookClient:
    """
    DynamoDB-backed runbook store.

    Usage:
        client = RunbookClient()
        runbook = client.get_latest(signature)
        client.put(runbook)
        client.record_outcome(signature, version, success=True, recovery_ms=4500)
    """

    _dynamodb: Any
    _table: Any

    def __init__(self) -> None:
        kwargs: dict[str, Any] = {
            "region_name": settings.aws_region,
        }
        if settings.aws_endpoint_url:
            kwargs["endpoint_url"] = settings.aws_endpoint_url

        self._dynamodb = boto3.resource("dynamodb", **kwargs)
        self._table = self._dynamodb.Table(settings.memory_table)

    def get_latest_runbook(self, failure_signature: str) -> dict[str, Any] | None:
        """Convenience method for LangGraph nodes returning raw dict item."""
        try:
            response = self._table.query(
                KeyConditionExpression="failure_signature = :sig",
                ExpressionAttributeValues={":sig": failure_signature},
                ScanIndexForward=False,
                Limit=1,
            )
            items = response.get("Items", [])
            if not items:
                return None
            return cast(dict[str, Any], items[0])
        except ClientError as exc:
            logger.error("DynamoDB error querying raw runbook: %s", exc)
            return None

    def save_runbook(
        self,
        failure_signature: str,
        diagnosis: str,
        plan_steps: list[dict[str, Any]],
        rationale: str,
    ) -> bool:
        """Convenience method for LangGraph nodes writing raw dict item."""
        now_str = datetime.now(UTC).isoformat()
        sig_obj = (
            FailureSignature.from_str(failure_signature)
            if ":" in failure_signature and len(failure_signature.split(":")) >= 4
            else FailureSignature(
                source_type="system",
                error_category="General",
                affected_resource=failure_signature,
                context_hash="0000",
            )
        )
        steps = [
            ActionStep(
                tool=s.get("tool", "system"),
                command=s.get("command", s.get("action", "")),
                action=s.get("action", s.get("command", "")),
                args=s.get("args", {}),
                dry_run=s.get("dry_run", True),
                dry_run_safe=s.get("dry_run_safe", True),
                description=s.get("description", ""),
            )
            for s in plan_steps
        ]
        rb = Runbook(
            failure_signature=sig_obj,
            version=self.next_version(failure_signature),
            diagnosis=diagnosis,
            discriminating_check=rationale,
            action_plan=steps,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
            times_used=1,
            times_succeeded=1,
            times_failed=0,
        )
        item = rb.to_dynamodb_item()
        item["failure_signature"] = failure_signature  # preserve exact string passed
        item["created_at"] = now_str
        item["last_used"] = now_str
        try:
            self._table.put_item(Item=item)
            return True
        except ClientError as exc:
            logger.error("DynamoDB error in save_runbook: %s", exc)
            return False

    def get(self, signature: FailureSignature | str, version: int) -> Runbook | None:
        """Retrieve a specific version of a runbook."""
        sig_key = _extract_sig_key(signature)
        try:
            resp = self._table.get_item(
                Key={
                    "failure_signature": sig_key,
                    "version": version,
                }
            )
            item = resp.get("Item")
            if not item:
                return None
            return Runbook.from_dynamodb_item(item)
        except ClientError as exc:
            logger.error("DynamoDB error getting runbook %s v%d: %s", sig_key, version, exc)
            return None

    def get_latest(self, signature: FailureSignature | str) -> Runbook | None:
        """
        Retrieve the latest version of a runbook for a given failure signature.

        Returns None if no runbook exists.
        """
        sig_key = _extract_sig_key(signature)
        try:
            response = self._table.query(
                KeyConditionExpression="failure_signature = :sig",
                ExpressionAttributeValues={":sig": sig_key},
                ScanIndexForward=False,  # descending — newest first
                Limit=1,
            )
            items = response.get("Items", [])
            if not items:
                logger.debug("No runbook found for signature: %s", sig_key)
                return None

            runbook = Runbook.from_dynamodb_item(items[0])
            logger.info(
                "Retrieved runbook for %s (v%d, confidence=%.2f)",
                sig_key,
                runbook.version,
                runbook.confidence,
            )
            return runbook

        except ClientError as e:
            logger.error("DynamoDB error retrieving runbook: %s", e)
            return None

    def list_for_signature(self, signature: FailureSignature | str) -> list[Runbook]:
        """List all versions of runbooks for a signature, newest first."""
        sig_key = _extract_sig_key(signature)
        try:
            response = self._table.query(
                KeyConditionExpression="failure_signature = :sig",
                ExpressionAttributeValues={":sig": sig_key},
                ScanIndexForward=False,
            )
            items = response.get("Items", [])
            return [Runbook.from_dynamodb_item(it) for it in items]
        except ClientError as exc:
            logger.error("DynamoDB error listing runbooks for %s: %s", sig_key, exc)
            return []

    def put(self, runbook: Runbook) -> bool:
        """
        Write a runbook to DynamoDB.

        Returns True on success, False on failure.
        """
        try:
            item = runbook.to_dynamodb_item()
            self._table.put_item(Item=item)
            logger.info(
                "Saved runbook for %s v%d",
                runbook.failure_signature.key,
                runbook.version,
            )
            return True
        except ClientError as e:
            logger.error("DynamoDB error saving runbook: %s", e)
            return False

    def delete(self, signature: FailureSignature | str, version: int) -> bool:
        """Delete a specific runbook version."""
        sig_key = _extract_sig_key(signature)
        try:
            self._table.delete_item(
                Key={
                    "failure_signature": sig_key,
                    "version": version,
                }
            )
            logger.info("Deleted runbook %s v%d", sig_key, version)
            return True
        except ClientError as exc:
            logger.error("DynamoDB error deleting runbook %s v%d: %s", sig_key, version, exc)
            return False

    def record_outcome(
        self,
        signature: FailureSignature | str,
        version: int,
        success: bool,
        recovery_ms: float = 0.0,
    ) -> bool:
        """
        Update a runbook's stats after use.

        Updates: times_used, times_succeeded/times_failed, avg_recovery_ms, last_used.
        """
        sig_key = _extract_sig_key(signature)
        now_str = datetime.now(UTC).isoformat()
        try:
            # Query existing stats to update avg_recovery_ms accurately
            existing = self.get(sig_key, version)
            new_avg = recovery_ms
            if existing and existing.times_used > 0 and recovery_ms > 0:
                new_avg = (
                    (existing.avg_recovery_ms * existing.times_used) + recovery_ms
                ) / (existing.times_used + 1)

            self._table.update_item(
                Key={
                    "failure_signature": sig_key,
                    "version": version,
                },
                UpdateExpression="""
                    SET times_used = times_used + :one,
                        times_succeeded = times_succeeded + :succ,
                        times_failed = times_failed + :fail,
                        avg_recovery_ms = :avg_rec,
                        last_used = :now
                """,
                ExpressionAttributeValues={
                    ":one": 1,
                    ":succ": 1 if success else 0,
                    ":fail": 0 if success else 1,
                    ":avg_rec": int(new_avg),
                    ":now": now_str,
                },
            )
            logger.info(
                "Recorded outcome for %s v%d: success=%s, recovery=%.0fms",
                sig_key,
                version,
                success,
                recovery_ms,
            )
            return True
        except ClientError as e:
            logger.error("DynamoDB error recording outcome: %s", e)
            return False

    def next_version(self, signature: FailureSignature | str) -> int:
        """Return the next version number for a given signature (1 if new)."""
        sig_key = _extract_sig_key(signature)
        try:
            response = self._table.query(
                KeyConditionExpression="failure_signature = :sig",
                ExpressionAttributeValues={":sig": sig_key},
                ScanIndexForward=False,
                Limit=1,
                ProjectionExpression="#v",
                ExpressionAttributeNames={"#v": "version"},
            )
            items = response.get("Items", [])
            if not items:
                return 1
            return int(items[0]["version"]) + 1
        except ClientError:
            return 1
