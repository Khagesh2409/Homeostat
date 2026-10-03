"""Scenario 6: Network Partition — Drops traffic via isolating NetworkPolicy and verifies LLM diagnosis and rule removal."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl

DENY_ALL_POLICY_JSON = json.dumps({
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "chaos-isolate-policy", "namespace": "default"},
    "spec": {
        "podSelector": {"matchLabels": {"app": "chaos-partition-app"}},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [],
        "egress": [],
    },
})


def create_network_partition_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    target_namespace: str = "default",
    policy_name: str = "chaos-isolate-policy",
    app_label: str = "chaos-partition-app",
) -> ChaosScenario:
    """Create the Network Partition chaos scenario."""

    def setup() -> None:
        # Create deployment and service to receive traffic
        execute_kubectl(
            f"create deployment {app_label} --image=nginx:alpine -n {target_namespace}",
            kubectl_fn,
        )
        execute_kubectl(
            f"delete networkpolicy {policy_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    def attack() -> None:
        # Apply isolating network policy that drops all ingress and egress
        execute_kubectl(
            f"apply -f - <<EOF\n{DENY_ALL_POLICY_JSON}\nEOF",
            kubectl_fn,
        )

    def verify_recovered() -> bool:
        # Check if the blocking network policy was identified and removed/relaxed
        out = execute_kubectl(
            f"get networkpolicy {policy_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )
        # If the policy is deleted or not found, recovery succeeded
        return policy_name not in out or "NotFound" in out or not out.strip()

    def cleanup() -> None:
        execute_kubectl(
            f"delete networkpolicy {policy_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )
        execute_kubectl(
            f"delete deployment {app_label} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    return ChaosScenario(
        name="network_partition",
        category="network",
        description="Drops traffic to a service via isolating NetworkPolicy; LLM diagnoses and removes bad rule",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=60.0,
        expected_recovery_max_s=180.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_network_partition_scenario()
