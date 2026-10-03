"""
Unit tests for the memory client and schemas.
Uses moto to mock DynamoDB — no real AWS or LocalStack needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from homeostat.memory.schemas import ActionStep, FailureSignature, Runbook

# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set fake AWS credentials so moto doesn't complain."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("HOMEOSTAT_AWS_ENDPOINT_URL", "")   # unset LocalStack URL


@pytest.fixture()
def dynamodb_table() -> Iterator[Any]:
    """Create a mock DynamoDB table using moto."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="homeostat-runbooks",
            KeySchema=[
                {"AttributeName": "failure_signature", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "failure_signature", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


# ── FailureSignature tests ─────────────────────────────────────

class TestFailureSignature:
    def test_key_format(self) -> None:
        sig = FailureSignature(
            source_type="pod",
            error_category="CrashLoopBackOff",
            affected_resource="deployment/nginx",
            context_hash="ab12",
        )
        assert sig.key == "pod:CrashLoopBackOff:deployment/nginx:ab12"

    def test_make_normalizes_source_type(self) -> None:
        sig = FailureSignature.make(
            source_type="POD",
            error_category="OOMKilled",
            affected_resource="deployment/api",
        )
        assert sig.source_type == "pod"

    def test_same_context_produces_same_hash(self) -> None:
        sig1 = FailureSignature.make(
            source_type="pod",
            error_category="CrashLoopBackOff",
            affected_resource="deployment/nginx",
            context={"namespace": "default"},
        )
        sig2 = FailureSignature.make(
            source_type="pod",
            error_category="CrashLoopBackOff",
            affected_resource="deployment/nginx",
            context={"namespace": "default"},
        )
        assert sig1.key == sig2.key

    def test_different_context_produces_different_hash(self) -> None:
        sig1 = FailureSignature.make(
            source_type="pod",
            error_category="CrashLoopBackOff",
            affected_resource="deployment/nginx",
            context={"namespace": "default"},
        )
        sig2 = FailureSignature.make(
            source_type="pod",
            error_category="CrashLoopBackOff",
            affected_resource="deployment/nginx",
            context={"namespace": "production"},
        )
        assert sig1.key != sig2.key

    def test_str_returns_key(self) -> None:
        sig = FailureSignature(
            source_type="disk",
            error_category="DiskPressure",
            affected_resource="node/*",
            context_hash="0000",
        )
        assert str(sig) == sig.key


# ── Runbook tests ──────────────────────────────────────────────

class TestRunbook:
    def _make_runbook(self, times_used: int = 0, times_succeeded: int = 0) -> Runbook:
        sig = FailureSignature(
            source_type="pod",
            error_category="CrashLoopBackOff",
            affected_resource="deployment/nginx",
            context_hash="ab12",
        )
        return Runbook(
            failure_signature=sig,
            version=1,
            diagnosis="Pod is crash-looping due to bad config",
            discriminating_check="Verify pod restarts > 3 in last 5 minutes",
            action_plan=[
                ActionStep(
                    tool="kubectl_tool", action="rollout_restart", args={"deployment": "nginx"}
                ),
            ],
            times_used=times_used,
            times_succeeded=times_succeeded,
        )

    def test_success_rate_zero_when_unused(self) -> None:
        rb = self._make_runbook()
        assert rb.success_rate == 0.0

    def test_success_rate_calculation(self) -> None:
        rb = self._make_runbook(times_used=4, times_succeeded=3)
        assert rb.success_rate == 0.75

    def test_confidence_is_low_with_few_uses(self) -> None:
        # 100% success rate but only 1 use → low confidence
        rb = self._make_runbook(times_used=1, times_succeeded=1)
        assert rb.confidence < 0.5

    def test_confidence_grows_with_use(self) -> None:
        rb_low = self._make_runbook(times_used=1, times_succeeded=1)
        rb_high = self._make_runbook(times_used=10, times_succeeded=10)
        assert rb_high.confidence > rb_low.confidence

    def test_dynamodb_roundtrip(self) -> None:
        """Serialize → deserialize should produce an equivalent runbook."""
        rb = self._make_runbook(times_used=5, times_succeeded=4)
        item = rb.to_dynamodb_item()
        restored = Runbook.from_dynamodb_item(item)

        assert restored.failure_signature.key == rb.failure_signature.key
        assert restored.version == rb.version
        assert restored.diagnosis == rb.diagnosis
        assert restored.discriminating_check == rb.discriminating_check
        assert len(restored.action_plan) == len(rb.action_plan)
        assert restored.action_plan[0].tool == rb.action_plan[0].tool
        assert restored.times_used == rb.times_used
        assert restored.times_succeeded == rb.times_succeeded


# ── RunbookClient tests (with mocked DynamoDB) ────────────────

class TestRunbookClient:
    def test_get_returns_none_when_empty(self, dynamodb_table: None) -> None:
        with mock_aws():
            # Re-import inside mock context
            import importlib

            import homeostat.memory.client as mod
            importlib.reload(mod)
            client = mod.RunbookClient()

            sig = FailureSignature(
                source_type="pod",
                error_category="CrashLoopBackOff",
                affected_resource="deployment/nginx",
                context_hash="ab12",
            )
            result = client.get_latest(sig)
            assert result is None

    def test_put_and_get_roundtrip(self, dynamodb_table: None) -> None:
        with mock_aws():
            import importlib

            import homeostat.memory.client as mod
            importlib.reload(mod)
            client = mod.RunbookClient()

            sig = FailureSignature(
                source_type="pod",
                error_category="CrashLoopBackOff",
                affected_resource="deployment/nginx",
                context_hash="ab12",
            )
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="Test diagnosis",
                discriminating_check="Check restart count > 3",
                action_plan=[
                    ActionStep(tool="kubectl_tool", action="delete_pod", args={"pod": "nginx-xyz"}),
                ],
            )

            success = client.put(rb)
            assert success is True

            retrieved = client.get_latest(sig)
            assert retrieved is not None
            assert retrieved.diagnosis == "Test diagnosis"
            assert retrieved.version == 1
