"""
Tier-0 playbooks: deterministic fixes for boring incidents. No LLM involved.

Each playbook is pure data plus small pure functions:

  match    -> does this alert (plus log signatures) fit the playbook?
  target   -> the cooldown key, i.e. *what* we are about to touch
  actions  -> the ActionSteps to run (fed into the normal dry_run/execute path)
  verify   -> read-only ActionSteps that prove the fix worked

Actions are expressed as ``ActionStep`` objects rather than callables so that
Tier-0 plans flow through exactly the same dry-run, execute, and audit path
as LLM-generated plans. The tool layer (Step 2.3) interprets the commands.

Scope rules (mirrors the tool layer):
  * Pod playbooks never touch ``kube-system``. The only kube-system action
    Tier-0 may take is restarting CoreDNS.
  * Nothing here restarts Prometheus, Alertmanager, the watchdog, or the
    agent's own namespace.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from homeostat.config import settings
from homeostat.preprocessor.schemas import ErrorSignature
from homeostat.state import ActionStep, Alert

Matcher = Callable[[Alert, Sequence[ErrorSignature]], bool]
StepBuilder = Callable[[Alert], list[ActionStep]]
TargetBuilder = Callable[[Alert], str]


@dataclass(frozen=True)
class Tier0Playbook:
    """A deterministic remediation recipe."""

    name: str
    description: str
    alertnames: frozenset[str]
    match: Matcher
    target: TargetBuilder
    actions: StepBuilder
    verify: StepBuilder
    max_retries: int = 2
    cooldown_seconds: int = field(default_factory=lambda: settings.tier0_cooldown_seconds)
    window_seconds: int = 900

    def matches(self, alert: Alert, signatures: Sequence[ErrorSignature] = ()) -> bool:
        """Cheap alertname gate first, then the playbook specific predicate."""
        return alert.alertname in self.alertnames and self.match(alert, signatures)


# ── Shared helpers ────────────────────────────────────────────────────────────

# Namespaces Tier-0 must never modify via pod/deployment playbooks.
PROTECTED_NAMESPACES = frozenset({"kube-system", "homeostat"})

# Workloads Tier-0 must never bounce, regardless of namespace.
_PROTECTED_WORKLOAD = re.compile(r"prometheus|alertmanager|watchdog|homeostat", re.IGNORECASE)

# Kubernetes generates pod suffixes from a vowel free alphabet, which lets us
# strip them without mangling real names like "my-app-redis".
_K8S_SAFE = "[bcdfghjklmnpqrstvwxz2456789]"
_DEPLOYMENT_POD = re.compile(rf"^(?P<base>.+)-{_K8S_SAFE}{{6,10}}-{_K8S_SAFE}{{5}}$")
_DAEMONSET_POD = re.compile(rf"^(?P<base>.+)-{_K8S_SAFE}{{5}}$")

_RESTART_IN_TEXT = re.compile(r"restarts?\s*[=:]?\s*(\d+)", re.IGNORECASE)

# Log categories where restarting or rescheduling cannot help.
_UNRESTARTABLE_CATEGORIES = frozenset({"ImagePullBackOff", "FailedScheduling"})

POD_RESTART_MAX_RESTARTS = 5
DISK_TARGET_PERCENT = 80
CLUSTER_DNS_PROBE = "kubernetes.default.svc.cluster.local"


def pod_name(alert: Alert) -> str | None:
    """Resolve the pod an alert is about, or None if it isn't pod scoped."""
    if pod := alert.labels.get("pod"):
        return pod
    if alert.source.startswith("pod/"):
        name = alert.source.removeprefix("pod/")
        return name or None
    return None


def workload_name(pod: str) -> str:
    """
    Collapse a pod name to its owning workload.

    Replacement pods get new names, so cooldown must key on the workload or a
    crashlooping Deployment would dodge the cooldown on every restart.
    """
    for pattern in (_DEPLOYMENT_POD, _DAEMONSET_POD):
        if m := pattern.match(pod):
            return m.group("base")
    return pod  # StatefulSet pods (db-0) and bare pods keep their name


def restart_count(alert: Alert) -> int | None:
    """Best effort restart count from labels, annotations, or the message."""
    for source in (alert.labels, alert.annotations):
        for key in ("restarts", "restart_count"):
            raw = source.get(key)
            if raw is not None:
                try:
                    return int(float(raw))
                except ValueError:
                    continue
    if m := _RESTART_IN_TEXT.search(alert.message):
        return int(m.group(1))
    return None


def node_name(alert: Alert) -> str | None:
    """Resolve the node for node scoped alerts (kube-state-metrics or node-exporter)."""
    if node := alert.labels.get("node"):
        return node
    if instance := alert.labels.get("instance"):
        return instance.split(":", 1)[0]
    if alert.source.startswith("node/") and alert.source != "node/*":
        return alert.source.removeprefix("node/")
    return None


def deployment_name(alert: Alert) -> str | None:
    """Resolve a single deployment from an alert's labels."""
    return alert.labels.get("deployment") or alert.labels.get("service") or None


def _signature_workload(sig: ErrorSignature) -> str:
    """Workload a clustered signature belongs to ("pod/api-<POD>" and "deployment/api" -> "api")."""
    name = sig.source.split("/", 1)[-1].removesuffix("-<POD>")
    return workload_name(name)


def _signatures_for(alert: Alert, signatures: Sequence[ErrorSignature]) -> list[ErrorSignature]:
    pod = pod_name(alert)
    if not pod:
        return []
    workload = workload_name(pod)
    return [
        s for s in signatures
        if (not s.namespace or s.namespace == alert.namespace)
        and _signature_workload(s) == workload
    ]


def _has_unrestartable_logs(alert: Alert, signatures: Sequence[ErrorSignature]) -> bool:
    return any(s.category in _UNRESTARTABLE_CATEGORIES for s in _signatures_for(alert, signatures))


def _is_coredns(alert: Alert) -> bool:
    if alert.namespace != "kube-system":
        return False
    pod = pod_name(alert) or ""
    return pod.startswith("coredns") or alert.labels.get("deployment") == "coredns"


def _pod_target(alert: Alert) -> str:
    pod = pod_name(alert) or "unknown"
    return f"{alert.namespace}/workload/{workload_name(pod)}"


def _wait_workload_ready(alert: Alert, timeout_seconds: int = 60) -> list[ActionStep]:
    pod = pod_name(alert) or "unknown"
    return [
        ActionStep(
            tool="kubectl",
            command="wait_workload_ready",
            args={
                "namespace": alert.namespace,
                "workload": workload_name(pod),
                "timeout_seconds": timeout_seconds,
            },
            description=f"Wait up to {timeout_seconds}s for {workload_name(pod)} to be Running",
        )
    ]


def _delete_pod(alert: Alert, why: str) -> list[ActionStep]:
    pod = pod_name(alert) or "unknown"
    return [
        ActionStep(
            tool="kubectl",
            command="delete_pod",
            args={"namespace": alert.namespace, "name": pod},
            description=f"Delete pod {alert.namespace}/{pod} ({why})",
        )
    ]


# ── pod-restart ───────────────────────────────────────────────────────────────


def _match_pod_restart(alert: Alert, signatures: Sequence[ErrorSignature]) -> bool:
    if not pod_name(alert) or alert.namespace in PROTECTED_NAMESPACES:
        return False
    restarts = restart_count(alert)
    if restarts is not None and restarts >= POD_RESTART_MAX_RESTARTS:
        return False  # past the point where a restart is a plausible fix
    return not _has_unrestartable_logs(alert, signatures)


POD_RESTART = Tier0Playbook(
    name="pod-restart",
    description=f"CrashLoopBackOff under {POD_RESTART_MAX_RESTARTS} restarts: delete the pod",
    alertnames=frozenset({"KubePodCrashLooping", "CrashLoopBackOff", "BackOff"}),
    match=_match_pod_restart,
    target=_pod_target,
    actions=lambda a: _delete_pod(a, "CrashLoopBackOff restart"),
    verify=_wait_workload_ready,
)


# ── pod-reschedule ────────────────────────────────────────────────────────────


def _match_pod_reschedule(alert: Alert, signatures: Sequence[ErrorSignature]) -> bool:
    if not pod_name(alert) or alert.namespace in PROTECTED_NAMESPACES:
        return False
    return not _has_unrestartable_logs(alert, signatures)


POD_RESCHEDULE = Tier0Playbook(
    name="pod-reschedule",
    description="Evicted or OOMKilled pod: delete it and let the scheduler place a fresh one",
    alertnames=frozenset({"KubePodOOMKilled", "OOMKilled", "KubePodEvicted", "Evicted"}),
    match=_match_pod_reschedule,
    target=_pod_target,
    actions=lambda a: _delete_pod(a, "reschedule after eviction or OOM"),
    verify=_wait_workload_ready,
)


# ── disk-cleanup ──────────────────────────────────────────────────────────────


def _match_disk_cleanup(alert: Alert, signatures: Sequence[ErrorSignature]) -> bool:
    return node_name(alert) is not None


def _disk_cleanup_actions(alert: Alert) -> list[ActionStep]:
    node = node_name(alert) or "unknown"
    selector = f"spec.nodeName={node}"
    return [
        ActionStep(
            tool="kubectl",
            command="delete_pods_by_phase",
            args={"phase": "Succeeded", "all_namespaces": True, "field_selector": selector},
            description=f"Delete completed pods on {node}",
        ),
        ActionStep(
            tool="kubectl",
            command="delete_pods_by_phase",
            args={"phase": "Failed", "all_namespaces": True, "field_selector": selector},
            description=f"Delete failed and evicted pods on {node}",
        ),
        ActionStep(
            tool="system",
            command="prune_images",
            args={"node": node},
            dry_run_safe=False,  # crictl rmi --prune has no dry-run mode
            description=f"Prune unused container images on {node}",
        ),
    ]


def _disk_cleanup_verify(alert: Alert) -> list[ActionStep]:
    node = node_name(alert) or "unknown"
    return [
        ActionStep(
            tool="system",
            command="check_disk_usage",
            args={"node": node, "max_percent": DISK_TARGET_PERCENT},
            description=f"Confirm disk usage on {node} is below {DISK_TARGET_PERCENT}%",
        )
    ]


DISK_CLEANUP = Tier0Playbook(
    name="disk-cleanup",
    description="Disk pressure: delete completed pods and prune unused images",
    alertnames=frozenset({
        "NodeDiskPressure",
        "NodeDiskCritical",
        "NodeFilesystemSpaceFillingUp",
        "NodeFilesystemAlmostOutOfSpace",
    }),
    match=_match_disk_cleanup,
    target=lambda a: f"node/{node_name(a) or 'unknown'}",
    actions=_disk_cleanup_actions,
    verify=_disk_cleanup_verify,
)


# ── service-bounce ────────────────────────────────────────────────────────────


def _match_service_bounce(alert: Alert, signatures: Sequence[ErrorSignature]) -> bool:
    name = deployment_name(alert)
    if not name or alert.namespace in PROTECTED_NAMESPACES:
        return False
    job = alert.labels.get("job", "")
    return not (_PROTECTED_WORKLOAD.search(name) or _PROTECTED_WORKLOAD.search(job))


def _service_bounce_actions(alert: Alert) -> list[ActionStep]:
    name = deployment_name(alert) or "unknown"
    return [
        ActionStep(
            tool="kubectl",
            command="rollout_restart",
            args={"namespace": alert.namespace, "kind": "deployment", "name": name},
            description=f"Rollout restart deployment {alert.namespace}/{name}",
        )
    ]


def _service_bounce_verify(alert: Alert) -> list[ActionStep]:
    name = deployment_name(alert) or "unknown"
    return [
        ActionStep(
            tool="kubectl",
            command="wait_rollout",
            args={"namespace": alert.namespace, "kind": "deployment", "name": name,
                  "timeout_seconds": 120},
            description=f"Wait for {name} rollout to complete",
        ),
        ActionStep(
            tool="prometheus",
            command="check_target_up",
            args={"namespace": alert.namespace, "job": alert.labels.get("job", name)},
            description=f"Confirm Prometheus can scrape {name} again",
        ),
    ]


SERVICE_BOUNCE = Tier0Playbook(
    name="service-bounce",
    description="Single scrape target down: rollout restart its deployment",
    alertnames=frozenset({"TargetDown"}),
    match=_match_service_bounce,
    target=lambda a: f"{a.namespace}/deployment/{deployment_name(a) or 'unknown'}",
    actions=_service_bounce_actions,
    verify=_service_bounce_verify,
)


# ── dns-restart ───────────────────────────────────────────────────────────────


def _match_dns_restart(alert: Alert, signatures: Sequence[ErrorSignature]) -> bool:
    return alert.alertname == "CoreDNSDown" or _is_coredns(alert)


def _dns_restart_actions(alert: Alert) -> list[ActionStep]:
    return [
        ActionStep(
            tool="kubectl",
            command="rollout_restart",
            args={"namespace": "kube-system", "kind": "deployment", "name": "coredns"},
            description="Rollout restart CoreDNS",
        )
    ]


def _dns_restart_verify(alert: Alert) -> list[ActionStep]:
    return [
        ActionStep(
            tool="kubectl",
            command="wait_rollout",
            args={"namespace": "kube-system", "kind": "deployment", "name": "coredns",
                  "timeout_seconds": 60},
            description="Wait for CoreDNS rollout to complete",
        ),
        ActionStep(
            tool="system",
            command="dns_lookup",
            args={"hostname": CLUSTER_DNS_PROBE},
            description=f"Resolve {CLUSTER_DNS_PROBE} from inside the cluster",
        ),
    ]


DNS_RESTART = Tier0Playbook(
    name="dns-restart",
    description="CoreDNS not ready: rollout restart the CoreDNS deployment",
    alertnames=frozenset({
        "CoreDNSDown",
        "KubePodNotReady",
        "KubePodCrashLooping",
        "KubeDeploymentReplicasMismatch",
    }),
    match=_match_dns_restart,
    target=lambda a: "kube-system/deployment/coredns",
    actions=_dns_restart_actions,
    verify=_dns_restart_verify,
)


# Order matters: first match wins. DNS goes first so a crashlooping CoreDNS pod
# gets the CoreDNS playbook rather than the generic pod-restart.
PLAYBOOKS: tuple[Tier0Playbook, ...] = (
    DNS_RESTART,
    POD_RESTART,
    POD_RESCHEDULE,
    DISK_CLEANUP,
    SERVICE_BOUNCE,
)
