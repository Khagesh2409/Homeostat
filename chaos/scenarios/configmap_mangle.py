"""Scenario 3: ConfigMap Mangle — Corrupts a ConfigMap and verifies LLM restoration from backup."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl

VALID_CONFIG = "PORT=8080\nLOG_LEVEL=INFO"
MANGLED_CONFIG = "PORT=INVALID_NAN\nSYNTAX_ERROR:::CORRUPT"


def create_configmap_mangle_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    target_namespace: str = "default",
    configmap_name: str = "chaos-app-config",
) -> ChaosScenario:
    """Create the ConfigMap Mangle chaos scenario."""

    def setup() -> None:
        execute_kubectl(
            f"create configmap {configmap_name} -n {target_namespace} "
            f'--from-literal=app.env="{VALID_CONFIG}"',
            kubectl_fn,
        )

    def attack() -> None:
        execute_kubectl(
            f"patch configmap {configmap_name} -n {target_namespace} "
            f'--type=merge -p \'{{"data":{{"app.env":"{MANGLED_CONFIG}"}}}}\'',
            kubectl_fn,
        )

    def verify_recovered() -> bool:
        out = execute_kubectl(
            f"get configmap {configmap_name} -n {target_namespace} -o json",
            kubectl_fn,
        )
        try:
            data = json.loads(out)
            config_data = data.get("data", {})
            env_val = str(config_data.get("app.env", ""))
            # Recovered if config is no longer mangled and has valid PORT definition
            return bool("PORT=8080" in env_val and "SYNTAX_ERROR" not in env_val)
        except Exception:  # noqa: BLE001
            return False

    def cleanup() -> None:
        execute_kubectl(
            f"delete configmap {configmap_name} -n {target_namespace} --ignore-not-found=true",
            kubectl_fn,
        )

    return ChaosScenario(
        name="configmap_mangle",
        category="config",
        description="Corrupt a ConfigMap value; LLM diagnoses and restores from known-good backup",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=60.0,
        expected_recovery_max_s=180.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_configmap_mangle_scenario()
