"""Scenario 1: Pod Kill — Deletes a random worker pod and verifies self-healing rescheduling."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import structlog

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl

logger = structlog.get_logger(__name__)


def create_pod_kill_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    target_namespace: str = "default",
    deployment_name: str = "chaos-worker",
) -> ChaosScenario:
    """Create the Pod Kill chaos scenario."""

    def setup() -> None:
        # Create deployment if not present
        execute_kubectl(
            f"create deployment {deployment_name} --image=nginx:alpine -n {target_namespace}",
            kubectl_fn,
        )
        execute_kubectl(
            f"rollout status deployment/{deployment_name} -n {target_namespace} --timeout=60s",
            kubectl_fn,
        )

    def attack() -> None:
        out = execute_kubectl(
            f"get pods -n {target_namespace} -l app={deployment_name} -o json",
            kubectl_fn,
        )
        pod_name = ""
        try:
            data = json.loads(out)
            items = data.get("items", [])
            if items:
                pod_name = items[0].get("metadata", {}).get("name", "")
        except Exception as e:  # noqa: BLE001
            logger.debug("pod_json_parse_failed", error=str(e))

        if pod_name:
            execute_kubectl(
                f"delete pod {pod_name} -n {target_namespace} --grace-period=0 --force",
                kubectl_fn,
            )
        else:
            execute_kubectl(
                f"delete pod -n {target_namespace} -l app={deployment_name} --grace-period=0",
                kubectl_fn,
            )

    def verify_recovered() -> bool:
        out = execute_kubectl(
            f"get deployment {deployment_name} -n {target_namespace} -o json",
            kubectl_fn,
        )
        try:
            data = json.loads(out)
            status = data.get("status", {})
            ready_replicas = status.get("readyReplicas", 0)
            return bool(ready_replicas >= 1)
        except Exception:  # noqa: BLE001
            return False

    def cleanup() -> None:
        execute_kubectl(
            f"delete deployment {deployment_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    return ChaosScenario(
        name="pod_kill",
        category="pod",
        description="Random worker pod deletion via kubectl; verifies Tier-0 detects and pod is rescheduled",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=30.0,
        expected_recovery_max_s=60.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_pod_kill_scenario()
