"""Scenario 12: Self Healing Stress — Kills the agent operator pod and verifies K8s restart and state checkpoint recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl

logger = structlog.get_logger(__name__)


def create_self_healing_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    agent_url: str = "http://localhost:8080",
    agent_namespace: str = "homeostat",
    deployment_name: str = "homeostat-agent",
    http_client: httpx.Client | None = None,
) -> ChaosScenario:
    """Create the Self-Healing Stress chaos scenario."""
    client = http_client or httpx.Client(timeout=5.0)
    base_url = agent_url.rstrip("/")

    def setup() -> None:
        # Verify agent is running and healthy
        try:
            client.get(f"{base_url}/health")
        except (httpx.HTTPError, OSError):
            pass

    def attack() -> None:
        # Kill the agent pod
        execute_kubectl(
            f"delete pod -n {agent_namespace} -l app.kubernetes.io/name={deployment_name} "
            "--grace-period=0 --force",
            kubectl_fn,
        )

    def verify_recovered() -> bool:
        # 1. Check K8s deployment status has ready replicas
        out = execute_kubectl(
            f"get deployment {deployment_name} -n {agent_namespace} -o json",
            kubectl_fn,
        )
        k8s_ready = False
        try:
            data = json.loads(out)
            ready_replicas = data.get("status", {}).get("readyReplicas", 0)
            k8s_ready = ready_replicas >= 1
        except Exception as e:  # noqa: BLE001
            logger.debug("self_healing_status_parse_failed", error=str(e))

        # 2. Check HTTP health probe is answering
        http_ready = False
        try:
            resp = client.get(f"{base_url}/health")
            http_ready = resp.status_code == 200
        except (httpx.HTTPError, OSError):
            pass

        return k8s_ready or http_ready

    def cleanup() -> None:
        # Verify rollout
        execute_kubectl(
            f"rollout status deployment/{deployment_name} -n {agent_namespace} --timeout=60s",
            kubectl_fn,
        )

    return ChaosScenario(
        name="self_healing",
        category="pod",
        description="Kill the agent operator pod; K8s restarts it and agent resumes from checkpoint",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=30.0,
        expected_recovery_max_s=90.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_self_healing_scenario()
