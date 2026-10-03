"""Scenario 5: OOM Kill — Deploys a memory-hungry pod and verifies Tier-0 eviction / limit enforcement."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl


def create_oom_kill_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    target_namespace: str = "default",
    pod_name: str = "chaos-oom-pod",
) -> ChaosScenario:
    """Create the OOM Kill chaos scenario."""

    def setup() -> None:
        execute_kubectl(
            f"delete pod {pod_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    def attack() -> None:
        # Launch a pod with strict 20Mi memory limit that attempts to allocate 100Mi
        cmd = (
            f"run {pod_name} -n {target_namespace} --image=busybox --restart=Never "
            '--limits="memory=20Mi" '
            "-- /bin/sh -c 'x=\"\"; while true; do x=\"$x$x$x$x$x$x$x$x$x$x\"; done'"
        )
        execute_kubectl(cmd, kubectl_fn)

    def verify_recovered() -> bool:
        out = execute_kubectl(
            f"get pod {pod_name} -n {target_namespace} -o json",
            kubectl_fn,
        )
        try:
            data = json.loads(out)
            status = data.get("status", {})
            container_statuses = status.get("containerStatuses", [])
            if not container_statuses:
                return True

            terminated = container_statuses[0].get("lastState", {}).get("terminated") or container_statuses[0].get("state", {}).get("terminated")
            if terminated and terminated.get("reason") == "OOMKilled":
                # Tier-0 detected and evicted/handled
                return True

            phase = str(status.get("phase", ""))
            return bool(phase in {"Succeeded", "Failed"})
        except Exception:  # noqa: BLE001
            # If pod was completely deleted/evicted, recovery succeeded
            return bool("NotFound" in out or not out.strip())

    def cleanup() -> None:
        execute_kubectl(
            f"delete pod {pod_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    return ChaosScenario(
        name="oom_kill",
        category="pod",
        description="Deploy memory-hungry pod; Tier-0 eviction or memory limit enforcement",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=30.0,
        expected_recovery_max_s=90.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_oom_kill_scenario()
