"""
Unit tests for the memory client, schemas, retriever, and writer.
Uses moto to mock DynamoDB — no real AWS or LocalStack needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from homeostat.memory.client import RunbookClient
from homeostat.memory.retrieval import RunbookRetriever
from homeostat.memory.schemas import ActionStep, FailureSignature, Runbook
from homeostat.memory.writer import RunbookWriter
from homeostat.state import (
    ActionPlan,
    AgentState,
    Alert,
    AlertSeverity,
)
from homeostat.state import (
    ActionStep as StateActionStep,
)
from homeostat.tools.runner import CommandResult, CommandRunner

# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set fake AWS credentials so moto doesn't complain."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("HOMEOSTAT_AWS_ENDPOINT_URL", "")  # unset LocalStack URL


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

    def test_from_str(self) -> None:
        key = "pod:CrashLoopBackOff:deployment/nginx:ab12"
        sig = FailureSignature.from_str(key)
        assert sig.source_type == "pod"
        assert sig.error_category == "CrashLoopBackOff"
        assert sig.affected_resource == "deployment/nginx"
        assert sig.context_hash == "ab12"
        assert sig.key == key

    def test_from_str_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid FailureSignature key"):
            FailureSignature.from_str("pod:CrashLoopBackOff")

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


# ── ActionStep and Runbook tests ───────────────────────────────


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

    def test_action_step_aliases(self) -> None:
        step1 = ActionStep(tool="kubectl", command="delete_pod", dry_run_safe=True)
        assert step1.action == "delete_pod"
        assert step1.dry_run is True

        step2 = ActionStep(tool="helm", action="rollback", dry_run=False)
        assert step2.command == "rollback"
        assert step2.dry_run_safe is False

    def test_success_rate_zero_when_unused(self) -> None:
        rb = self._make_runbook()
        assert rb.success_rate == 0.0

    def test_success_rate_calculation(self) -> None:
        rb = self._make_runbook(times_used=4, times_succeeded=3)
        assert rb.success_rate == 0.75

    def test_confidence_is_low_with_few_uses(self) -> None:
        rb = self._make_runbook(times_used=1, times_succeeded=1)
        assert rb.confidence < 0.5

    def test_confidence_grows_with_use(self) -> None:
        rb_low = self._make_runbook(times_used=1, times_succeeded=1)
        rb_high = self._make_runbook(times_used=10, times_succeeded=10)
        assert rb_high.confidence > rb_low.confidence

    def test_dynamodb_roundtrip(self) -> None:
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


# ── RunbookClient CRUD Tests ───────────────────────────────────


class TestRunbookClient:
    def test_get_returns_none_when_empty(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            assert client.get_latest(sig) is None
            assert client.get(sig, 1) is None

    def test_put_and_get_roundtrip(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="Test diagnosis",
                discriminating_check="Check restart count > 3",
                action_plan=[ActionStep(tool="kubectl", command="delete_pod", args={"name": "p1"})],
            )
            assert client.put(rb) is True

            retrieved = client.get_latest(sig)
            assert retrieved is not None
            assert retrieved.diagnosis == "Test diagnosis"
            assert retrieved.version == 1

            # Get exact version
            v1 = client.get(sig, 1)
            assert v1 is not None
            assert v1.version == 1

    def test_list_and_delete(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")

            rb1 = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="V1",
                discriminating_check="",
                action_plan=[],
            )
            rb2 = Runbook(
                failure_signature=sig,
                version=2,
                diagnosis="V2",
                discriminating_check="",
                action_plan=[],
            )
            client.put(rb1)
            client.put(rb2)

            all_vers = client.list_for_signature(sig)
            assert len(all_vers) == 2
            assert all_vers[0].version == 2  # descending

            # Delete v1
            assert client.delete(sig, 1) is True
            assert client.get(sig, 1) is None
            latest = client.get_latest(sig)
            assert latest is not None
            assert latest.version == 2

    def test_record_outcome_updates_stats(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="Test",
                discriminating_check="",
                action_plan=[],
                times_used=1,
                times_succeeded=1,
                avg_recovery_ms=1000.0,
            )
            client.put(rb)

            assert client.record_outcome(sig, 1, success=True, recovery_ms=3000.0) is True
            updated = client.get(sig, 1)
            assert updated is not None
            assert updated.times_used == 2
            assert updated.times_succeeded == 2
            assert updated.avg_recovery_ms == 2000.0

    def test_next_version(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            assert client.next_version(sig) == 1

            client.put(
                Runbook(
                    failure_signature=sig,
                    version=1,
                    diagnosis="",
                    discriminating_check="",
                    action_plan=[],
                )
            )
            assert client.next_version(sig) == 2


# ── RunbookRetriever Tests (Lookup + Confirmation) ─────────────


class TestRunbookRetriever:
    def test_retrieve_not_found(self, dynamodb_table: None) -> None:
        with mock_aws():
            retriever = RunbookRetriever()
            res = retriever.retrieve("pod:CrashLoopBackOff:deployment/nginx:ab12")
            assert res.found is False
            assert res.confirmed is False
            assert res.runbook is None

    def test_retrieve_low_confidence_rejected(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            # 1 use -> confidence = 1.0 * (1/10) = 0.1 < 0.6 threshold
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="Low confidence",
                discriminating_check="",
                action_plan=[],
                times_used=1,
                times_succeeded=1,
            )
            client.put(rb)

            retriever = RunbookRetriever(client=client, min_confidence=0.6)
            res = retriever.retrieve(sig)
            assert res.found is True
            assert res.confirmed is False
            assert "below threshold" in (res.rejection_reason or "")

    def test_retrieve_confirmed_with_passing_check(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="High confidence",
                discriminating_check="restarts > 3",
                action_plan=[],
                times_used=10,
                times_succeeded=10,  # confidence = 1.0 >= 0.6
            )
            client.put(rb)

            retriever = RunbookRetriever(client=client, min_confidence=0.6)
            context = {"restart_count": 5}
            res = retriever.retrieve(sig, context=context)

            assert res.found is True
            assert res.confirmed is True
            assert res.runbook is not None
            assert retriever.precision == 1.0
            assert retriever.confirmed_retrievals == 1

    def test_retrieve_rejected_with_failing_check_wrong_scenario(
        self, dynamodb_table: None
    ) -> None:
        """Test wrong-retrieval scenario: check fails -> discarded, precision reflects failure."""
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="DNS failure expected",
                discriminating_check="verify DNS resolution is failing",
                action_plan=[],
                times_used=10,
                times_succeeded=9,  # high confidence
            )
            client.put(rb)

            retriever = RunbookRetriever(client=client, min_confidence=0.6)
            # Context shows DNS is NOT failing (wrong runbook scenario!)
            context = {"dns_failing": False}
            res = retriever.retrieve(sig, context=context)

            assert res.found is True
            assert res.confirmed is False
            assert "Discriminating check failed" in (res.rejection_reason or "")
            assert retriever.total_retrievals == 1
            assert retriever.confirmed_retrievals == 0
            assert retriever.rejected_retrievals == 1
            assert retriever.precision == 0.0

    def test_retrieval_precision_tracking(self, dynamodb_table: None) -> None:
        with mock_aws():
            client = RunbookClient()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            rb = Runbook(
                failure_signature=sig,
                version=1,
                diagnosis="Test",
                discriminating_check="restarts > 2",
                action_plan=[],
                times_used=10,
                times_succeeded=10,
            )
            client.put(rb)

            retriever = RunbookRetriever(client=client, min_confidence=0.6)

            # 1. Successful confirmation
            res1 = retriever.retrieve(sig, context={"restart_count": 4})
            assert res1.confirmed is True

            # 2. Failed confirmation
            res2 = retriever.retrieve(sig, context={"restart_count": 1})
            assert res2.confirmed is False

            assert retriever.total_retrievals == 2
            assert retriever.confirmed_retrievals == 1
            assert retriever.rejected_retrievals == 1
            assert retriever.precision == 0.5

            retriever.reset_metrics()
            assert retriever.total_retrievals == 0
            assert retriever.precision == 1.0

    def test_confirm_discriminating_check_command_runner(self) -> None:
        mock_runner = MagicMock(spec=CommandRunner)
        mock_runner.run.return_value = CommandResult(
            cmd=["nslookup", "kubernetes.default"],
            returncode=0,
            stdout="Address: 10.96.0.1",
            stderr="",
            elapsed_seconds=0.1,
        )

        retriever = RunbookRetriever(runner=mock_runner)
        rb = Runbook(
            failure_signature=FailureSignature("pod", "Crash", "r1", "0000"),
            version=1,
            diagnosis="",
            discriminating_check="cmd: nslookup kubernetes.default",
            action_plan=[],
        )

        passed, out = retriever.confirm_discriminating_check(rb, {})
        assert passed is True
        assert "10.96.0.1" in out


# ── RunbookWriter Tests ───────────────────────────────────────


class TestRunbookWriter:
    def test_write_creates_v1_when_new(self, dynamodb_table: None) -> None:
        with mock_aws():
            writer = RunbookWriter()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            steps = [ActionStep(tool="kubectl", command="rollout_restart", args={"name": "nginx"})]

            rb = writer.write_runbook(
                signature=sig,
                diagnosis="Config error",
                action_plan=steps,
                discriminating_check="restarts > 3",
                recovery_ms=1200.0,
            )

            assert rb.version == 1
            assert rb.times_used == 1
            assert rb.times_succeeded == 1
            assert rb.avg_recovery_ms == 1200.0

            stored = writer.client.get(sig, 1)
            assert stored is not None
            assert stored.diagnosis == "Config error"

    def test_write_identical_plan_updates_stats_same_version(
        self, dynamodb_table: None
    ) -> None:
        with mock_aws():
            writer = RunbookWriter()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            steps = [ActionStep(tool="kubectl", command="delete_pod", args={"name": "p1"})]

            # 1. First write -> v1
            rb1 = writer.write_runbook(
                signature=sig,
                diagnosis="Initial",
                action_plan=steps,
                discriminating_check="check",
                recovery_ms=1000.0,
            )
            assert rb1.version == 1

            # 2. Second write with identical steps -> updates v1 stats
            rb2 = writer.write_runbook(
                signature=sig,
                diagnosis="Second execution",
                action_plan=steps,
                discriminating_check="check",
                recovery_ms=2000.0,
            )
            assert rb2.version == 1
            assert rb2.times_used == 2
            assert rb2.times_succeeded == 2

            v1 = writer.client.get(sig, 1)
            assert v1 is not None
            assert v1.times_used == 2

    def test_write_different_plan_increments_version(self, dynamodb_table: None) -> None:
        with mock_aws():
            writer = RunbookWriter()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")

            plan1 = [ActionStep(tool="kubectl", command="delete_pod", args={"name": "p1"})]
            plan2 = [ActionStep(tool="kubectl", command="rollout_restart", args={"name": "nginx"})]

            rb1 = writer.write_runbook(sig, "D1", plan1, "check1", recovery_ms=1000.0)
            assert rb1.version == 1

            rb2 = writer.write_runbook(sig, "D2", plan2, "check2", recovery_ms=800.0)
            assert rb2.version == 2

            assert writer.client.get(sig, 1) is not None
            assert writer.client.get(sig, 2) is not None
            latest = writer.client.get_latest(sig)
            assert latest is not None
            assert latest.version == 2

    def test_record_discriminating_check_failure_logs_without_deleting(
        self, dynamodb_table: None
    ) -> None:
        with mock_aws():
            writer = RunbookWriter()
            sig = FailureSignature("pod", "CrashLoopBackOff", "deployment/nginx", "ab12")
            writer.write_runbook(sig, "D1", [], "check", recovery_ms=1000.0)

            # Record failure of discriminating check
            writer.record_discriminating_check_failure(sig, 1, "DNS was not failing")

            # Runbook must still exist
            stored = writer.client.get(sig, 1)
            assert stored is not None
            assert stored.version == 1


# ── Graph Integration Tests ───────────────────────────────────


class TestMemoryGraphIntegration:
    def test_validate_with_passing_discriminating_check(self) -> None:
        from homeostat.nodes.validate import route_after_validate, validate

        alert = Alert(
            alertname="CrashLoopBackOff",
            severity=AlertSeverity.WARNING,
            namespace="default",
            source="deployment/nginx",
            message="",
            labels={"restart_count": "5"},
        )
        runbook = {
            "failure_signature": "pod:CrashLoopBackOff:deployment/nginx:ab12",
            "version": 1,
            "discriminating_check": "restarts >= 3",
        }
        state: AgentState = {
            "current_alert": alert,
            "failure_signature": "pod:CrashLoopBackOff:deployment/nginx:ab12",
            "retrieved_runbook": runbook,
        }

        # If context indicates restarts, it passes
        result = validate(state)
        assert "retrieved_runbook" not in result
        state.update(result)
        assert route_after_validate(state) == "execute"

    def test_validate_with_failing_discriminating_check(self) -> None:
        from homeostat.nodes.validate import route_after_validate, validate

        alert = Alert(
            alertname="CrashLoopBackOff",
            severity=AlertSeverity.WARNING,
            namespace="default",
            source="deployment/nginx",
            message="",
        )
        runbook = {
            "failure_signature": "pod:CrashLoopBackOff:deployment/nginx:ab12",
            "version": 1,
            "discriminating_check": "verify DNS resolution is failing",
        }
        state: AgentState = {
            "current_alert": alert,
            "failure_signature": "pod:CrashLoopBackOff:deployment/nginx:ab12",
            "retrieved_runbook": runbook,
        }

        # When alert is CrashLoopBackOff and discriminating check expects DNS failure,
        # it discards the runbook and routes to diagnose
        result = validate(state)
        assert result["retrieved_runbook"] is None
        state.update(result)
        assert route_after_validate(state) == "diagnose"

    def test_write_runbook_node_persists_via_writer(self, dynamodb_table: None) -> None:
        with mock_aws():
            from homeostat.nodes.write_runbook import write_runbook

            step = StateActionStep(
                tool="kubectl", command="rollout_restart", args={"name": "nginx"}
            )
            plan = ActionPlan(steps=[step], rationale="restarts > 3", generated_by="llm")
            state: AgentState = {
                "failure_signature": "pod:CrashLoopBackOff:deployment/nginx:ab12",
                "plan": plan,
                "diagnosis": "Memory pressure resolved",
            }

            res = write_runbook(state)
            assert res["outcome"] is not None

            client = RunbookClient()
            stored = client.get_latest("pod:CrashLoopBackOff:deployment/nginx:ab12")
            assert stored is not None
            assert stored.diagnosis == "Memory pressure resolved"
