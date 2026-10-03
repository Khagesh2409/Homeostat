"""
Tests for Tier-0 deterministic playbooks, the registry, and cooldown tracking.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from homeostat.preprocessor.schemas import ErrorSignature
from homeostat.state import (
    ActionPlan,
    ActionResult,
    ActionStep,
    AgentState,
    Alert,
    AlertSeverity,
)
from homeostat.tier0.cooldown import CooldownTracker, default_tracker
from homeostat.tier0.playbooks import (
    DISK_CLEANUP,
    DNS_RESTART,
    POD_RESCHEDULE,
    POD_RESTART,
    SERVICE_BOUNCE,
    node_name,
    restart_count,
    workload_name,
)
from homeostat.tier0.registry import (
    cooldown_key,
    find_playbook,
    is_tier0_candidate,
    resolve_playbook,
)

# ── Fixtures & helpers ────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _reset_default_tracker() -> Iterator[None]:
    default_tracker.reset()
    yield
    default_tracker.reset()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker(clock: FakeClock) -> CooldownTracker:
    return CooldownTracker(clock=clock)


def make_alert(
    alertname: str,
    *,
    namespace: str = "default",
    source: str = "unknown",
    message: str = "",
    labels: dict[str, str] | None = None,
) -> Alert:
    return Alert(
        alertname=alertname,
        severity=AlertSeverity.CRITICAL,
        namespace=namespace,
        source=source,
        message=message,
        labels={"alertname": alertname, **(labels or {})},
    )


def make_signature(source: str, category: str, namespace: str = "default") -> ErrorSignature:
    now = datetime.now(UTC)
    return ErrorSignature(
        source=source,
        category=category,
        pattern_hash="deadbeef",
        sample_message="sample",
        count=3,
        first_seen=now,
        last_seen=now,
        namespace=namespace,
    )


CRASHLOOP = make_alert(
    "KubePodCrashLooping",
    source="api-7d9f8b6c5d-xkqp2",
    labels={"pod": "api-7d9f8b6c5d-xkqp2", "container": "api"},
)


# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pod", "expected"),
    [
        ("api-7d9f8b6c5d-xkqp2", "api"),          # Deployment pod
        ("my-app-redis-7d9f8b6c5d-xkqp2", "my-app-redis"),
        ("node-exporter-x7k2p", "node-exporter"),  # DaemonSet pod
        ("postgres-0", "postgres-0"),              # StatefulSet keeps ordinal
        ("my-app-redis", "my-app-redis"),          # vowels mean it's not a k8s suffix
    ],
)
def test_workload_name(pod: str, expected: str) -> None:
    assert workload_name(pod) == expected


def test_restart_count_sources() -> None:
    assert restart_count(make_alert("X", labels={"restarts": "3"})) == 3
    assert restart_count(make_alert("X", message="container restarts: 7")) == 7
    assert restart_count(make_alert("X")) is None


def test_node_name_sources() -> None:
    assert node_name(make_alert("X", labels={"node": "ip-10-0-1-157"})) == "ip-10-0-1-157"
    assert node_name(make_alert("X", labels={"instance": "10.0.1.157:9100"})) == "10.0.1.157"
    assert node_name(make_alert("X", source="node/*")) is None


# ── Cooldown ──────────────────────────────────────────────────────────────────


def _check(tracker: CooldownTracker, key: str = "k") -> bool:
    return tracker.check(key, cooldown_seconds=60, max_attempts=2, window_seconds=900).allowed


def test_cooldown_blocks_repeat_within_window(tracker: CooldownTracker, clock: FakeClock) -> None:
    assert _check(tracker)
    tracker.record_attempt("k")

    clock.advance(30)
    decision = tracker.check("k", cooldown_seconds=60, max_attempts=2, window_seconds=900)
    assert not decision.allowed
    assert "cooling down" in decision.reason

    clock.advance(31)
    assert _check(tracker)


def test_cooldown_caps_attempts_in_window(tracker: CooldownTracker, clock: FakeClock) -> None:
    tracker.record_attempt("k")
    clock.advance(61)
    tracker.record_attempt("k")
    clock.advance(61)

    decision = tracker.check("k", cooldown_seconds=60, max_attempts=2, window_seconds=900)
    assert not decision.allowed
    assert "max attempts" in decision.reason

    clock.advance(900)  # both attempts age out
    assert _check(tracker)


def test_cooldown_is_per_key(tracker: CooldownTracker) -> None:
    tracker.record_attempt("a")
    assert not _check(tracker, "a")
    assert _check(tracker, "b")


def test_miss_blocks_until_window_expires(tracker: CooldownTracker, clock: FakeClock) -> None:
    tracker.record_miss("k")
    decision = tracker.check("k", cooldown_seconds=0, max_attempts=10, window_seconds=900)
    assert not decision.allowed
    assert "missed" in decision.reason

    clock.advance(901)
    assert tracker.check("k", cooldown_seconds=0, max_attempts=10, window_seconds=900).allowed


# ── pod-restart ───────────────────────────────────────────────────────────────


def test_pod_restart_fires_on_crashloop() -> None:
    assert find_playbook(CRASHLOOP) is POD_RESTART

    [step] = POD_RESTART.actions(CRASHLOOP)
    assert step.tool == "kubectl"
    assert step.command == "delete_pod"
    assert step.args == {"namespace": "default", "name": "api-7d9f8b6c5d-xkqp2"}
    assert step.dry_run_safe

    [check] = POD_RESTART.verify(CRASHLOOP)
    assert check.command == "wait_workload_ready"
    assert check.args["workload"] == "api"
    assert check.args["timeout_seconds"] == 60


def test_pod_restart_handles_k8s_event_source() -> None:
    alert = make_alert("BackOff", source="pod/web-7d9f8b6c5d-abcde")
    assert find_playbook(alert) is POD_RESTART
    assert POD_RESTART.actions(alert)[0].args["name"] == "web-7d9f8b6c5d-abcde"


def test_pod_restart_skips_when_restarts_too_high() -> None:
    alert = make_alert("KubePodCrashLooping", labels={"pod": "api-x", "restarts": "5"})
    assert not POD_RESTART.matches(alert)


def test_pod_restart_never_touches_kube_system() -> None:
    alert = make_alert("KubePodCrashLooping", namespace="kube-system",
                       labels={"pod": "metrics-server-7d9f8b6c5d-xkqp2"})
    assert find_playbook(alert) is None


def test_pod_restart_skips_image_pull_failures() -> None:
    sigs = [make_signature("deployment/api", "ImagePullBackOff")]
    assert not POD_RESTART.matches(CRASHLOOP, sigs)
    # Unrelated workload's image errors don't block it
    other = [make_signature("deployment/billing", "ImagePullBackOff")]
    assert POD_RESTART.matches(CRASHLOOP, other)


# ── pod-reschedule ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("alertname", ["KubePodOOMKilled", "OOMKilled", "Evicted"])
def test_pod_reschedule_fires_on_oom_and_eviction(alertname: str) -> None:
    alert = make_alert(alertname, labels={"pod": "worker-7d9f8b6c5d-xkqp2"})
    assert find_playbook(alert) is POD_RESCHEDULE

    [step] = POD_RESCHEDULE.actions(alert)
    assert step.command == "delete_pod"
    assert step.args == {"namespace": "default", "name": "worker-7d9f8b6c5d-xkqp2"}
    assert POD_RESCHEDULE.verify(alert)[0].args["workload"] == "worker"


def test_pod_reschedule_requires_a_pod() -> None:
    alert = make_alert("KubePodOOMKilled", source="deployment/worker")
    assert find_playbook(alert) is None


# ── disk-cleanup ──────────────────────────────────────────────────────────────


def test_disk_cleanup_fires_on_disk_pressure() -> None:
    alert = make_alert("NodeDiskPressure", source="node/*", labels={"node": "ip-10-0-1-157"})
    assert find_playbook(alert) is DISK_CLEANUP

    steps = DISK_CLEANUP.actions(alert)
    assert [s.command for s in steps] == [
        "delete_pods_by_phase",
        "delete_pods_by_phase",
        "prune_images",
    ]
    assert [s.args.get("phase") for s in steps[:2]] == ["Succeeded", "Failed"]
    assert all(s.args["field_selector"] == "spec.nodeName=ip-10-0-1-157" for s in steps[:2])
    assert steps[2].args == {"node": "ip-10-0-1-157"}
    assert steps[2].dry_run_safe is False  # image prune can't be simulated

    [check] = DISK_CLEANUP.verify(alert)
    assert check.command == "check_disk_usage"
    assert check.args == {"node": "ip-10-0-1-157", "max_percent": 80}


def test_disk_cleanup_needs_a_node() -> None:
    alert = make_alert("NodeDiskPressure", source="node/*")
    assert find_playbook(alert) is None


# ── service-bounce ────────────────────────────────────────────────────────────


def test_service_bounce_fires_on_single_target_down() -> None:
    alert = make_alert("TargetDown", namespace="shop", source="checkout",
                       labels={"job": "checkout", "service": "checkout"})
    assert find_playbook(alert) is SERVICE_BOUNCE

    [step] = SERVICE_BOUNCE.actions(alert)
    assert step.command == "rollout_restart"
    assert step.args == {"namespace": "shop", "kind": "deployment", "name": "checkout"}

    checks = SERVICE_BOUNCE.verify(alert)
    assert [c.command for c in checks] == ["wait_rollout", "check_target_up"]
    assert checks[1].tool == "prometheus"
    assert checks[1].args["job"] == "checkout"


@pytest.mark.parametrize(
    ("namespace", "labels"),
    [
        ("monitoring", {"job": "prometheus-k8s", "service": "prometheus-k8s"}),
        ("monitoring", {"job": "alertmanager-main", "service": "alertmanager-main"}),
        ("homeostat", {"job": "agent", "service": "agent"}),
        ("kube-system", {"job": "kube-proxy", "service": "kube-proxy"}),
        ("shop", {"job": "checkout"}),  # no service/deployment to bounce
    ],
)
def test_service_bounce_refuses_protected_or_unresolvable(
    namespace: str, labels: dict[str, str]
) -> None:
    alert = make_alert("TargetDown", namespace=namespace, labels=labels)
    assert find_playbook(alert) is None


# ── dns-restart ───────────────────────────────────────────────────────────────


def test_dns_restart_fires_on_coredns_down() -> None:
    alert = make_alert("CoreDNSDown", namespace="kube-system")
    assert find_playbook(alert) is DNS_RESTART

    [step] = DNS_RESTART.actions(alert)
    assert step.command == "rollout_restart"
    assert step.args == {"namespace": "kube-system", "kind": "deployment", "name": "coredns"}

    checks = DNS_RESTART.verify(alert)
    assert checks[-1].command == "dns_lookup"
    assert checks[-1].args["hostname"] == "kubernetes.default.svc.cluster.local"


def test_crashlooping_coredns_gets_dns_playbook_not_pod_restart() -> None:
    alert = make_alert("KubePodCrashLooping", namespace="kube-system",
                       labels={"pod": "coredns-6799fbcd5-x7k2p"})
    assert find_playbook(alert) is DNS_RESTART


def test_not_ready_pod_elsewhere_is_not_dns() -> None:
    alert = make_alert("KubePodNotReady", labels={"pod": "coredns-lookalike-x7k2p"})
    assert find_playbook(alert) is None


# ── Registry ──────────────────────────────────────────────────────────────────


def test_is_tier0_candidate() -> None:
    assert is_tier0_candidate(CRASHLOOP)
    assert is_tier0_candidate(make_alert("NodeDiskPressure"))
    assert not is_tier0_candidate(make_alert("KubeProxyDown"))


def test_resolve_returns_bound_match(tracker: CooldownTracker) -> None:
    res = resolve_playbook(CRASHLOOP, tracker=tracker)
    assert res.match is not None
    assert res.match.name == "pod-restart"
    assert res.match.target == "default/workload/api"
    assert res.match.actions[0].command == "delete_pod"
    assert res.match.verify_steps[0].command == "wait_workload_ready"


def test_resolve_respects_cooldown(tracker: CooldownTracker) -> None:
    tracker.record_attempt(cooldown_key("pod-restart", "default/workload/api"))
    res = resolve_playbook(CRASHLOOP, tracker=tracker)
    assert res.match is None
    assert "blocked" in res.reason


def test_replacement_pod_shares_cooldown(tracker: CooldownTracker) -> None:
    tracker.record_attempt(cooldown_key("pod-restart", "default/workload/api"))
    replacement = make_alert("KubePodCrashLooping", labels={"pod": "api-7d9f8b6c5d-zzzzz"})
    assert resolve_playbook(replacement, tracker=tracker).match is None


def test_resolve_unknown_alert(tracker: CooldownTracker) -> None:
    res = resolve_playbook(make_alert("KubeProxyDown"), tracker=tracker)
    assert res.match is None
    assert "No playbook matched" in res.reason


# ── Graph node integration ────────────────────────────────────────────────────


def test_tier0_node_builds_plan_and_records_attempt() -> None:
    from homeostat.nodes.tier0 import route_after_tier0, tier0

    state: AgentState = {"current_alert": CRASHLOOP}
    result = tier0(state)

    assert result["tier0_playbook_name"] == "pod-restart"
    assert result["plan"].generated_by == "tier0"
    assert result["plan"].steps[0].command == "delete_pod"
    assert result["verify_steps"][0].command == "wait_workload_ready"
    assert default_tracker.attempts(cooldown_key("pod-restart", "default/workload/api")) == 1

    state.update(result)
    assert route_after_tier0(state) == "dry_run"


def test_tier0_node_bails_during_cooldown() -> None:
    from homeostat.nodes.tier0 import route_after_tier0, tier0

    tier0({"current_alert": CRASHLOOP})
    second: AgentState = {"current_alert": CRASHLOOP}
    result = tier0(second)

    assert result["is_tier0"] is False
    assert any("cooling down" in line for line in result["incident_log"])
    second.update(result)
    assert route_after_tier0(second) == "memory_lookup"


def _failed_run(generated_by: str) -> AgentState:
    step = ActionStep(tool="kubectl", command="delete_pod")
    return {
        "current_alert": CRASHLOOP,
        "plan": ActionPlan(steps=[step], rationale="", generated_by=generated_by),
        "actions_taken": [ActionResult(step=step, success=False, error="boom")],
        "retry_count": 0,
        "max_retries": 3,
    }


@patch("homeostat.nodes.verify.time.sleep")
def test_verify_tier0_miss_hands_to_llm_and_stands_down(_sleep: object) -> None:
    from homeostat.nodes.verify import route_after_verify, verify

    state = _failed_run("tier0")
    state.update({"tier0_playbook_name": "pod-restart", "tier0_target": "default/workload/api"})

    result = verify(state)
    assert result["tier0_miss"] is True
    assert result["tier0_playbook_name"] == ""
    assert "retry_count" not in result  # Tier-0 miss doesn't eat the LLM's budget

    state.update(result)
    assert route_after_verify(state) == "diagnose"

    # Tier-0 now refuses this target even once the cooldown would have expired
    assert resolve_playbook(CRASHLOOP).match is None


@patch("homeostat.nodes.verify.time.sleep")
def test_verify_llm_failure_after_tier0_dry_run_is_not_a_miss(_sleep: object) -> None:
    from homeostat.nodes.verify import verify

    state = _failed_run("llm")
    state.update({"tier0_playbook_name": "pod-restart", "tier0_target": "default/workload/api"})

    result = verify(state)
    assert "tier0_miss" not in result
    assert result["retry_count"] == 1
    assert default_tracker.misses(cooldown_key("pod-restart", "default/workload/api")) == 0


@patch("homeostat.nodes.verify.time.sleep")
def test_verify_llm_failures_escalate_at_max_retries(_sleep: object) -> None:
    from homeostat.nodes.verify import route_after_verify, verify

    state = _failed_run("llm")
    routes = []
    for _ in range(3):
        state.update(verify(state))
        routes.append(route_after_verify(state))

    assert routes == ["diagnose", "diagnose", "escalate"]
