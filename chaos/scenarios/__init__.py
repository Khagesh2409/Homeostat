"""Chaos scenarios registry for Homeostat fault injection and safety testing."""

from __future__ import annotations

from framework.scenario import ChaosScenario
from scenarios.bedrock_unavailable import (
    create_bedrock_unavailable_scenario,
)
from scenarios.bedrock_unavailable import (
    scenario as bedrock_unavailable_scenario,
)
from scenarios.configmap_mangle import (
    create_configmap_mangle_scenario,
)
from scenarios.configmap_mangle import (
    scenario as configmap_mangle_scenario,
)
from scenarios.crash_loop import (
    create_crash_loop_scenario,
)
from scenarios.crash_loop import (
    scenario as crash_loop_scenario,
)
from scenarios.disk_fill import (
    create_disk_fill_scenario,
)
from scenarios.disk_fill import (
    scenario as disk_fill_scenario,
)
from scenarios.dns_failure import (
    create_dns_failure_scenario,
)
from scenarios.dns_failure import (
    scenario as dns_failure_scenario,
)
from scenarios.log_injection import (
    create_log_injection_scenario,
)
from scenarios.log_injection import (
    scenario as log_injection_scenario,
)
from scenarios.network_partition import (
    create_network_partition_scenario,
)
from scenarios.network_partition import (
    scenario as network_partition_scenario,
)
from scenarios.node_destroy import (
    create_node_destroy_scenario,
)
from scenarios.node_destroy import (
    scenario as node_destroy_scenario,
)
from scenarios.oom_kill import (
    create_oom_kill_scenario,
)
from scenarios.oom_kill import (
    scenario as oom_kill_scenario,
)
from scenarios.pod_kill import (
    create_pod_kill_scenario,
)
from scenarios.pod_kill import (
    scenario as pod_kill_scenario,
)
from scenarios.self_healing import (
    create_self_healing_scenario,
)
from scenarios.self_healing import (
    scenario as self_healing_scenario,
)
from scenarios.watchdog_test import (
    create_watchdog_test_scenario,
)
from scenarios.watchdog_test import (
    scenario as watchdog_test_scenario,
)

ALL_SCENARIOS: list[ChaosScenario] = [
    pod_kill_scenario,
    crash_loop_scenario,
    configmap_mangle_scenario,
    disk_fill_scenario,
    oom_kill_scenario,
    network_partition_scenario,
    dns_failure_scenario,
    bedrock_unavailable_scenario,
    node_destroy_scenario,
    log_injection_scenario,
    watchdog_test_scenario,
    self_healing_scenario,
]

SCENARIOS_MAP: dict[str, ChaosScenario] = {s.name: s for s in ALL_SCENARIOS}

__all__ = [
    "ALL_SCENARIOS",
    "SCENARIOS_MAP",
    "bedrock_unavailable_scenario",
    "configmap_mangle_scenario",
    "crash_loop_scenario",
    "create_bedrock_unavailable_scenario",
    "create_configmap_mangle_scenario",
    "create_crash_loop_scenario",
    "create_disk_fill_scenario",
    "create_dns_failure_scenario",
    "create_log_injection_scenario",
    "create_network_partition_scenario",
    "create_node_destroy_scenario",
    "create_oom_kill_scenario",
    "create_pod_kill_scenario",
    "create_self_healing_scenario",
    "create_watchdog_test_scenario",
    "disk_fill_scenario",
    "dns_failure_scenario",
    "log_injection_scenario",
    "network_partition_scenario",
    "node_destroy_scenario",
    "oom_kill_scenario",
    "pod_kill_scenario",
    "self_healing_scenario",
    "watchdog_test_scenario",
]
