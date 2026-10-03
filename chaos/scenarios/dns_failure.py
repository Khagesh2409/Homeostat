"""Scenario 7: DNS Failure — Kills CoreDNS pods and verifies Tier-0 automated rollout restart."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl


def create_dns_failure_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    dns_namespace: str = "kube-system",
    dns_label: str = "k8s-app=kube-dns",
    dns_deployment: str = "coredns",
) -> ChaosScenario:
    """Create the DNS Failure chaos scenario."""

    def setup() -> None:
        # Verify CoreDNS deployment is present and healthy
        execute_kubectl(
            f"rollout status deployment/{dns_deployment} -n {dns_namespace} --timeout=30s",
            kubectl_fn,
        )

    def attack() -> None:
        # Terminate CoreDNS pod(s)
        execute_kubectl(
            f"delete pod -n {dns_namespace} -l {dns_label} --grace-period=0 --force",
            kubectl_fn,
        )

    def verify_recovered() -> bool:
        out = execute_kubectl(
            f"get deployment/{dns_deployment} -n {dns_namespace} -o json",
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
        # Ensure CoreDNS is running
        execute_kubectl(
            f"rollout status deployment/{dns_deployment} -n {dns_namespace} --timeout=30s",
            kubectl_fn,
        )

    return ChaosScenario(
        name="dns_failure",
        category="pod",
        description="Kill CoreDNS pods; verifies Tier-0 CoreDNS rollout restart",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=30.0,
        expected_recovery_max_s=90.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_dns_failure_scenario()
