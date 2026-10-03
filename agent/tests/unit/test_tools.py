"""
Unit tests for agent tools: kubectl, terraform, helm, system, prometheus, memory,
runner, and scope enforcement.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from homeostat.state import ActionStep
from homeostat.tools.executor import execute_step
from homeostat.tools.helm import HelmTool
from homeostat.tools.kubectl import KubectlTool
from homeostat.tools.memory import MemoryTool
from homeostat.tools.prometheus import PrometheusTool
from homeostat.tools.runner import CommandResult, CommandRunner
from homeostat.tools.scope import check_scope
from homeostat.tools.system import SystemTool
from homeostat.tools.terraform import TerraformTool


def _cmd_res(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(
        cmd=[], returncode=returncode, stdout=stdout, stderr=stderr, elapsed_seconds=0.1
    )

# ── Scope Enforcement Tests ───────────────────────────────────────────────────


def test_scope_blocks_kube_system_mutations() -> None:
    # Delete pod in kube-system is blocked
    decision = check_scope(
        "kubectl", "delete_pod", {"namespace": "kube-system", "name": "kube-proxy"}
    )
    assert not decision.allowed
    assert "kube-system" in decision.reason

    # Apply in kube-system is blocked
    decision = check_scope("kubectl", "apply", {"namespace": "kube-system", "file": "test.yaml"})
    assert not decision.allowed


def test_scope_allows_coredns_restart_in_kube_system() -> None:
    decision = check_scope(
        "kubectl",
        "rollout_restart",
        {"namespace": "kube-system", "kind": "deployment", "name": "coredns"},
    )
    assert decision.allowed


def test_scope_blocks_homeostat_operator_mutations() -> None:
    decision = check_scope(
        "kubectl",
        "delete_pod",
        {"namespace": "homeostat", "name": "agent-7d9f8b-xkqp2"},
    )
    assert not decision.allowed
    assert "homeostat" in decision.reason


def test_scope_blocks_protected_monitoring_workloads() -> None:
    for workload in ("prometheus-server", "alertmanager-main", "watchdog-service"):
        decision = check_scope(
            "kubectl",
            "rollout_restart",
            {"namespace": "monitoring", "kind": "deployment", "name": workload},
        )
        assert not decision.allowed
        assert "protected" in decision.reason


def test_scope_allows_normal_workloads() -> None:
    decision = check_scope(
        "kubectl",
        "rollout_restart",
        {"namespace": "production", "kind": "deployment", "name": "frontend"},
    )
    assert decision.allowed


def test_scope_terraform_requires_homeostat_tag() -> None:
    # Unscoped apply blocked
    decision = check_scope("terraform", "apply", {"working_dir": "/infra"})
    assert not decision.allowed

    # Scoped apply allowed
    decision = check_scope(
        "terraform",
        "apply",
        {"working_dir": "/infra", "managed_by": "homeostat"},
    )
    assert decision.allowed

    # Tags containing managed-by: homeostat allowed
    decision = check_scope(
        "terraform",
        "destroy",
        {"working_dir": "/infra", "tags": {"managed-by": "homeostat"}},
    )
    assert decision.allowed


def test_scope_helm_blocks_protected_workloads_and_namespaces() -> None:
    # Block in kube-system
    decision = check_scope("helm", "upgrade", {"namespace": "kube-system", "release": "metrics"})
    assert not decision.allowed

    # Block monitoring release
    decision = check_scope(
        "helm", "upgrade", {"namespace": "default", "release": "prometheus-stack"}
    )
    assert not decision.allowed

    # Allow app release
    decision = check_scope("helm", "upgrade", {"namespace": "default", "release": "api-gateway"})
    assert decision.allowed


# ── CommandRunner Tests ───────────────────────────────────────────────────────


def test_runner_executes_successfully() -> None:
    def _mock_run(
        cmd: list[str], *, timeout_seconds: int = 30, input: str | None = None
    ) -> CommandResult:
        return _cmd_res(stdout="ok")

    runner = CommandRunner(runner=_mock_run)
    res = runner.run(["echo", "hello"])
    assert res.success
    assert res.stdout == "ok"


def test_runner_handles_failure() -> None:
    def _mock_run(
        cmd: list[str], *, timeout_seconds: int = 30, input: str | None = None
    ) -> CommandResult:
        return _cmd_res(stderr="error", returncode=1)

    runner = CommandRunner(runner=_mock_run)
    res = runner.run(["false"])
    assert not res.success
    assert res.stderr == "error"


# ── KubectlTool Tests ─────────────────────────────────────────────────────────


def test_kubectl_get_and_describe() -> None:
    mock_runner = MagicMock()
    mock_runner.run.return_value = _cmd_res(stdout=json.dumps({"kind": "PodList", "items": []}))
    tool = KubectlTool(runner=mock_runner)

    res = tool.get(resource="pods", namespace="default")
    assert res["success"]
    assert res["data"] == {"kind": "PodList", "items": []}

    mock_runner.run.return_value = _cmd_res(stdout="Details")
    desc = tool.describe(resource="pod", name="nginx", namespace="default")
    assert desc["success"]
    assert desc["output"] == "Details"


def test_kubectl_delete_pod_dry_run() -> None:
    mock_runner = MagicMock()
    mock_runner.run.return_value = _cmd_res(stdout="pod deleted (server dry run)")
    tool = KubectlTool(runner=mock_runner)

    # Dry-run flag added
    res = tool.delete_pod(name="nginx-123", namespace="default", dry_run=True)
    assert res["success"]
    assert res["dry_run"] is True
    called_cmd = mock_runner.run.call_args[0][0]
    assert "--dry-run=server" in called_cmd


def test_kubectl_delete_pods_by_phase() -> None:
    mock_runner = MagicMock()
    mock_runner.run.return_value = _cmd_res(stdout="deleted")
    tool = KubectlTool(runner=mock_runner)

    res = tool.delete_pods_by_phase(phase="Succeeded", all_namespaces=True, dry_run=True)
    assert res["success"]
    called_cmd = mock_runner.run.call_args[0][0]
    assert "--field-selector=status.phase=Succeeded" in called_cmd
    assert "--dry-run=server" in called_cmd


def test_kubectl_wait_workload_ready() -> None:
    mock_runner = MagicMock()
    pod_payload = {
        "items": [
            {
                "metadata": {"name": "api-abc"},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
        ]
    }
    mock_runner.run.return_value = CommandResult(
        cmd=[], returncode=0, stdout=json.dumps(pod_payload), stderr="", elapsed_seconds=0.1
    )
    tool = KubectlTool(runner=mock_runner)

    res = tool.wait_workload_ready(workload="api", namespace="default")
    assert res["success"]
    assert "ready=True" in res["output"]


# ── TerraformTool Tests ───────────────────────────────────────────────────────


def test_terraform_plan_and_apply() -> None:
    mock_runner = MagicMock()
    mock_runner.run.return_value = _cmd_res(stdout="Plan: 1 to add")
    tool = TerraformTool(runner=mock_runner)

    plan_res = tool.plan(working_dir="/infra")
    assert plan_res["success"]

    # Dry-run apply runs plan instead
    apply_dry = tool.apply(working_dir="/infra", dry_run=True)
    assert apply_dry["success"]
    called_cmd = mock_runner.run.call_args[0][0]
    assert "plan" in called_cmd

    # Real apply runs apply -auto-approve
    apply_real = tool.apply(working_dir="/infra", dry_run=False)
    assert apply_real["success"]
    called_cmd = mock_runner.run.call_args[0][0]
    assert "apply" in called_cmd
    assert "-auto-approve" in called_cmd


# ── HelmTool Tests ────────────────────────────────────────────────────────────


def test_helm_upgrade_and_rollback() -> None:
    mock_runner = MagicMock()
    mock_runner.run.return_value = _cmd_res(stdout="Release upgraded")
    tool = HelmTool(runner=mock_runner)

    # Dry-run upgrade adds --dry-run
    res = tool.upgrade(release="web", chart="./chart", namespace="default", dry_run=True)
    assert res["success"]
    called_cmd = mock_runner.run.call_args[0][0]
    assert "--dry-run" in called_cmd

    # Rollback adds --dry-run
    res_rb = tool.rollback(release="web", revision=1, namespace="default", dry_run=True)
    assert res_rb["success"]
    called_cmd_rb = mock_runner.run.call_args[0][0]
    assert "--dry-run" in called_cmd_rb


# ── SystemTool Tests ──────────────────────────────────────────────────────────


def test_system_disk_usage() -> None:
    tool = SystemTool()
    # Check current workspace disk usage
    res = tool.check_disk_usage(path=".", max_percent=99.9)
    assert res["success"]
    assert "used_percent" in res

    # Force threshold breach
    res_fail = tool.check_disk_usage(path=".", max_percent=0.0001)
    assert not res_fail["success"]


def test_system_dns_lookup() -> None:
    tool = SystemTool()
    res = tool.dns_lookup("localhost")
    assert res["success"]
    assert "127.0.0.1" in res["output"] or "::1" in res["output"]


def test_system_prune_images() -> None:
    mock_runner = MagicMock()
    mock_runner.run.return_value = _cmd_res(stdout="Pruned")
    tool = SystemTool(runner=mock_runner)

    res_dry = tool.prune_images(dry_run=True)
    assert res_dry["success"]
    assert "Would prune" in res_dry["output"]

    res_real = tool.prune_images(dry_run=False)
    assert res_real["success"]


# ── PrometheusTool Tests ──────────────────────────────────────────────────────


@patch("httpx.Client.get")
def test_prometheus_query_and_alerts(mock_get: MagicMock) -> None:
    tool = PrometheusTool(base_url="http://mock-prometheus:9090")

    # Mock query response
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "success",
        "data": {"result": [{"metric": {"__name__": "up"}, "value": [1000, "1"]}]},
    }
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    res = tool.query_metrics("up")
    assert res["success"]
    assert len(res["data"]) == 1

    # Mock alerts response
    mock_resp.json.return_value = {
        "status": "success",
        "data": {"alerts": [{"labels": {"alertname": "HighCPU"}, "state": "firing"}]},
    }
    alerts_res = tool.check_alert_status("HighCPU")
    assert alerts_res["success"]
    assert alerts_res["firing"] is True


@patch("httpx.Client.get")
def test_prometheus_check_target_up(mock_get: MagicMock) -> None:
    tool = PrometheusTool(base_url="http://mock-prometheus:9090")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "success",
        "data": {
            "activeTargets": [
                {"labels": {"job": "checkout"}, "health": "up"}
            ]
        },
    }
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    res = tool.check_target_up(job="checkout")
    assert res["success"]
    assert res["all_up"] if "all_up" in res else True


# ── MemoryTool Tests ──────────────────────────────────────────────────────────


def test_memory_tool_read_and_write() -> None:
    mock_client = MagicMock()
    mock_runbook = MagicMock()
    mock_runbook.to_dict.return_value = {"diagnosis": "test"}
    mock_client.get_runbook.return_value = mock_runbook
    mock_client.save_runbook.return_value = True

    tool = MemoryTool(client=mock_client)

    read_res = tool.read_runbook("test:sig")
    assert read_res["success"]

    # Dry-run write
    write_dry = tool.write_runbook("test:sig", "diag", [], "rationale", dry_run=True)
    assert write_dry["success"]
    assert not mock_client.save_runbook.called

    # Real write
    write_real = tool.write_runbook("test:sig", "diag", [], "rationale", dry_run=False)
    assert write_real["success"]
    assert mock_client.save_runbook.called


# ── Executor Dispatcher Tests ─────────────────────────────────────────────────


def test_executor_blocks_scope_violation() -> None:
    step = ActionStep(
        tool="kubectl",
        command="delete_pod",
        args={"namespace": "kube-system", "name": "etcd"},
    )
    res = execute_step(step, dry_run=False)
    assert not res.success
    assert "Scope violation" in res.error


def test_executor_simulates_unsafe_dry_run() -> None:
    step = ActionStep(
        tool="system",
        command="prune_images",
        args={"node": "node-1"},
        dry_run_safe=False,
    )
    res = execute_step(step, dry_run=True)
    assert res.success
    assert res.dry_run is True
    assert "simulated pass" in res.output


def test_executor_handles_unknown_tool() -> None:
    step = ActionStep(
        tool="unknown_tool",
        command="noop",
        args={},
    )
    res = execute_step(step, dry_run=False)
    assert not res.success
    assert "Unknown tool" in res.error
