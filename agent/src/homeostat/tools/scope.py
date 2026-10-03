"""
Scope enforcement for agent tools.

Strict safety rails:
1. `kubectl_tool` cannot modify the `kube-system` namespace (except CoreDNS restart).
2. `kubectl_tool` cannot modify `homeostat` system namespace.
3. No tool can modify or disable Prometheus, Alertmanager, or the watchdog.
4. `terraform_tool` can only manage resources tagged `managed-by: homeostat`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PROTECTED_NAMESPACES = frozenset({"kube-system", "homeostat"})
PROTECTED_WORKLOADS = re.compile(r"prometheus|alertmanager|watchdog|homeostat", re.IGNORECASE)

DESTRUCTIVE_KUBECTL_COMMANDS = frozenset({
    "delete",
    "delete_pod",
    "delete_pods",
    "delete_pods_by_phase",
    "rollout_restart",
    "apply",
    "patch",
    "scale",
})


@dataclass(frozen=True)
class ScopeDecision:
    """Outcome of safety check."""

    allowed: bool
    reason: str = ""


def check_scope(tool: str, command: str, args: dict[str, Any]) -> ScopeDecision:
    """Enforce scope restrictions across all tools."""
    tool_normalized = tool.lower().removesuffix("_tool")
    command_normalized = command.lower()

    if tool_normalized == "kubectl":
        return _check_kubectl_scope(command_normalized, args)
    elif tool_normalized == "terraform":
        return _check_terraform_scope(command_normalized, args)
    elif tool_normalized == "helm":
        return _check_helm_scope(command_normalized, args)
    elif tool_normalized in ("system", "prometheus", "memory"):
        return _check_generic_workload_protection(args)

    return ScopeDecision(True)


def _check_kubectl_scope(command: str, args: dict[str, Any]) -> ScopeDecision:
    namespace = args.get("namespace", "default")
    name = str(args.get("name", args.get("workload", "")))

    # CoreDNS exception in kube-system
    if namespace == "kube-system":
        is_coredns = name == "coredns" or "coredns" in name
        is_restart = command in ("rollout_restart", "restart")
        if is_coredns and is_restart:
            return ScopeDecision(True, "CoreDNS restart allowed in kube-system")
        return ScopeDecision(
            False,
            f"Modifying kube-system namespace is prohibited (attempted {command} on {name})",
        )

    if namespace == "homeostat" and command in DESTRUCTIVE_KUBECTL_COMMANDS:
        return ScopeDecision(
            False,
            f"Modifying homeostat operator namespace is prohibited (attempted {command})",
        )

    # Workload protection across all namespaces
    if command in DESTRUCTIVE_KUBECTL_COMMANDS:
        if PROTECTED_WORKLOADS.search(name):
            return ScopeDecision(
                False,
                f"Modifying protected monitoring or agent workload '{name}' is prohibited",
            )
        field_selector = str(args.get("field_selector", ""))
        if PROTECTED_WORKLOADS.search(field_selector):
            return ScopeDecision(
                False,
                "Field selector targets protected monitoring or agent workload",
            )

    return ScopeDecision(True)


def _check_terraform_scope(command: str, args: dict[str, Any]) -> ScopeDecision:
    # Any destructive or mutating terraform actions (apply, destroy) must be scoped
    if command in ("apply", "destroy"):
        managed_by = args.get("managed_by", "")
        tags = args.get("tags", {})
        target = args.get("target", "")

        is_tagged = (
            managed_by == "homeostat"
            or tags.get("managed-by") == "homeostat"
            or "managed-by: homeostat" in str(args)
        )
        has_safe_target = bool(target and "homeostat" in target.lower())

        if not (is_tagged or has_safe_target):
            return ScopeDecision(
                False,
                "Terraform actions can only touch resources tagged 'managed-by: homeostat'",
            )

    return ScopeDecision(True)


def _check_helm_scope(command: str, args: dict[str, Any]) -> ScopeDecision:
    release = str(args.get("release", args.get("name", "")))
    namespace = args.get("namespace", "default")

    if namespace in PROTECTED_NAMESPACES and not (
        namespace == "kube-system" and "coredns" in release
    ):
        return ScopeDecision(
            False,
            f"Helm operations prohibited in protected namespace '{namespace}'",
        )

    if PROTECTED_WORKLOADS.search(release):
        return ScopeDecision(
            False,
            f"Helm operation targeting protected workload '{release}' is prohibited",
        )

    return ScopeDecision(True)


def _check_generic_workload_protection(args: dict[str, Any]) -> ScopeDecision:
    target = str(args.get("target", args.get("name", args.get("job", ""))))
    action = str(args.get("action", ""))
    if action in ("stop", "disable", "kill") and PROTECTED_WORKLOADS.search(target):
        return ScopeDecision(
            False,
            f"Disabling protected workload '{target}' is prohibited",
        )
    return ScopeDecision(True)
