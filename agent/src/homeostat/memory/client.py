"""
DynamoDB client for runbook storage.

Read/write runbooks to DynamoDB. Works against both real AWS and LocalStack.
The endpoint_url is None in prod (real AWS) and set to LocalStack URL in dev.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError

from homeostat.config import settings
from homeostat.memory.schemas import FailureSignature, Runbook

logger = logging.getLogger(__name__)


class RunbookClient:
    """
    DynamoDB-backed runbook store.

    Usage:
        client = RunbookClient()
        runbook = await client.get_latest(signature)
        await client.put(runbook)
        await client.record_outcome(signature, version, success=True, recovery_ms=4500)
    """

    def __init__(self) -> None:
        kwargs: dict[str, Any] = {
            "region_name": settings.aws_region,
        }
        # In dev, point at LocalStack
        if settings.aws_endpoint_url:
            kwargs["endpoint_url"] = settings.aws_endpoint_url

        self._dynamodb = boto3.resource("dynamodb", **kwargs)
        self._table = self._dynamodb.Table(settings.memory_table)

    def get_latest_runbook(self, failure_signature: str) -> dict[str, Any] | None:
        """Convenience method for LangGraph nodes."""
        # We don't have the full FailureSignature components, so we just use it as the key
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
        except ClientError:
            return None

    def save_runbook(
        self,
        failure_signature: str,
        diagnosis: str,
        plan_steps: list[dict[str, Any]],
        rationale: str,
    ) -> bool:
        """Convenience method for LangGraph nodes."""
        item = {
            "failure_signature": failure_signature,
            "version": 1,
            "diagnosis": diagnosis,
            "action_plan": plan_steps,
            "discriminating_check": rationale,
            "times_used": 0,
            "times_succeeded": 0,
            "times_failed": 0,
            "created_at": "now",
            "last_used": "now",
        }
        try:
            self._table.put_item(Item=item)
            return True
        except ClientError:
            return False

    def get_latest(self, signature: FailureSignature) -> Runbook | None:
        """
        Retrieve the latest version of a runbook for a given failure signature.

        Returns None if no runbook exists.
        """
        try:
            # Query for all versions of this signature, sorted descending
            response = self._table.query(
                KeyConditionExpression="failure_signature = :sig",
                ExpressionAttributeValues={":sig": signature.key},
                ScanIndexForward=False,  # descending — newest first
                Limit=1,
            )
            items = response.get("Items", [])
            if not items:
                logger.debug("No runbook found for signature: %s", signature.key)
                return None

            runbook = Runbook.from_dynamodb_item(items[0])
            logger.info(
                "Retrieved runbook for %s (v%d, confidence=%.2f)",
                signature.key,
                runbook.version,
                runbook.confidence,
            )
            return runbook

        except ClientError as e:
            logger.error("DynamoDB error retrieving runbook: %s", e)
            return None

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

    def record_outcome(
        self,
        signature: FailureSignature,
        version: int,
        success: bool,
        recovery_ms: float,
    ) -> bool:
        """
        Update a runbook's stats after use.

        Updates: times_used, times_succeeded/times_failed, avg_recovery_ms, last_used.
        """
        try:
            # Atomic update — safe even if multiple agents run (shouldn't happen, but defensive)
            self._table.update_item(
                Key={
                    "failure_signature": signature.key,
                    "version": version,
                },
                UpdateExpression="""
                    SET times_used = times_used + :one,
                        times_succeeded = times_succeeded + :succ,
                        times_failed = times_failed + :fail,
                        last_used = :now
                """,
                ExpressionAttributeValues={
                    ":one": 1,
                    ":succ": 1 if success else 0,
                    ":fail": 0 if success else 1,
                    ":now": __import__("datetime").datetime.utcnow().isoformat(),
                },
            )
            logger.info(
                "Recorded outcome for %s v%d: success=%s, recovery=%.0fms",
                signature.key,
                version,
                success,
                recovery_ms,
            )
            return True
        except ClientError as e:
            logger.error("DynamoDB error recording outcome: %s", e)
            return False

    def next_version(self, signature: FailureSignature) -> int:
        """Return the next version number for a given signature (1 if new)."""
        try:
            response = self._table.query(
                KeyConditionExpression="failure_signature = :sig",
                ExpressionAttributeValues={":sig": signature.key},
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
