"""Scenario 2: Crash Loop — Deploys a crashlooping pod/bad image and verifies automated rollback."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl


def create_crash_loop_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    target_namespace: str = "default",
    deployment_name: str = "chaos-crashloop-app",
    valid_image: str = "nginx:alpine",
    bad_image: str = "homeostat.local/nonexistent-image:invalid",
) -> ChaosScenario:
    """Create the Crash Loop chaos scenario."""

    def setup() -> None:
        execute_kubectl(
            f"create deployment {deployment_name} --image={valid_image} -n {target_namespace}",
            kubectl_fn,
        )
        execute_kubectl(
            f"rollout status deployment/{deployment_name} -n {target_namespace} --timeout=60s",
            kubectl_fn,
        )

    def attack() -> None:
        execute_kubectl(
            f"set image deployment/{deployment_name} {deployment_name}={bad_image} -n {target_namespace}",
            kubectl_fn,
        )

    def verify_recovered() -> bool:
        out = execute_kubectl(
            f"get deployment {deployment_name} -n {target_namespace} -o json",
            kubectl_fn,
        )
        try:
            data = json.loads(out)
            spec = data.get("spec", {})
            containers = spec.get("template", {}).get("spec", {}).get("containers", [])
            current_image = containers[0].get("image", "") if containers else ""
            status = data.get("status", {})
            ready_replicas = status.get("readyReplicas", 0)

            # Recovered if image rolled back away from bad image and has ready replicas
            is_valid = current_image != bad_image and ready_replicas >= 1
            return bool(is_valid)
        except Exception:  # noqa: BLE001
            return False

    def cleanup() -> None:
        execute_kubectl(
            f"delete deployment {deployment_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    return ChaosScenario(
        name="crash_loop",
        category="pod",
        description="Deploy pod with bad image / crashlooping; Tier-0 or LLM rollback",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=45.0,
        expected_recovery_max_s=120.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_crash_loop_scenario()
