"""
Kubectl tool wrapper.

Supports get, describe, delete_pod, delete_pods_by_phase, rollout_restart,
wait_rollout, wait_workload_ready, apply, and logs.
Enforces dry-run with `--dry-run=server` for mutating commands.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeostat.tools.runner import CommandRunner, default_runner

logger = logging.getLogger(__name__)


class KubectlTool:
    """Interface to kubectl CLI."""

    def __init__(self, runner: CommandRunner = default_runner) -> None:
        self._runner = runner

    def run_command(
        self,
        command: str,
        args: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Dispatch kubectl operations."""
        cmd_name = command.lower()
        if cmd_name == "get":
            return self.get(
                resource=str(args.get("resource", "")),
                name=args.get("name"),
                namespace=args.get("namespace"),
                output=args.get("output", "json"),
            )
        elif cmd_name == "describe":
            return self.describe(
                resource=str(args.get("resource", "")),
                name=str(args.get("name", "")),
                namespace=args.get("namespace"),
            )
        elif cmd_name == "delete_pod":
            return self.delete_pod(
                name=str(args.get("name", "")),
                namespace=str(args.get("namespace", "default")),
                dry_run=dry_run,
            )
        elif cmd_name == "delete_pods_by_phase":
            return self.delete_pods_by_phase(
                phase=str(args.get("phase", "")),
                all_namespaces=bool(args.get("all_namespaces", True)),
                field_selector=args.get("field_selector"),
                dry_run=dry_run,
            )
        elif cmd_name == "rollout_restart":
            return self.rollout_restart(
                kind=str(args.get("kind", "deployment")),
                name=str(args.get("name", "")),
                namespace=str(args.get("namespace", "default")),
                dry_run=dry_run,
            )
        elif cmd_name == "wait_rollout":
            return self.wait_rollout(
                kind=str(args.get("kind", "deployment")),
                name=str(args.get("name", "")),
                namespace=str(args.get("namespace", "default")),
                timeout_seconds=int(args.get("timeout_seconds", 60)),
            )
        elif cmd_name == "wait_workload_ready":
            return self.wait_workload_ready(
                workload=str(args.get("workload", "")),
                namespace=str(args.get("namespace", "default")),
                timeout_seconds=int(args.get("timeout_seconds", 60)),
            )
        elif cmd_name == "apply":
            return self.apply(
                filepath=args.get("file", args.get("filepath")),
                content=args.get("content"),
                namespace=args.get("namespace"),
                dry_run=dry_run,
            )
        elif cmd_name == "logs":
            return self.logs(
                name=str(args.get("name", args.get("pod", ""))),
                namespace=str(args.get("namespace", "default")),
                container=args.get("container"),
                tail=int(args.get("tail", 100)),
            )
        else:
            return {
                "success": False,
                "error": f"Unsupported kubectl command: {command}",
                "output": "",
            }

    def get(
        self,
        resource: str,
        name: str | None = None,
        namespace: str | None = None,
        output: str = "json",
    ) -> dict[str, Any]:
        cmd = ["kubectl", "get", resource]
        if name:
            cmd.append(name)
        if namespace:
            cmd.extend(["-n", namespace])
        if output:
            cmd.extend(["-o", output])

        res = self._runner.run(cmd)
        if not res.success:
            return {"success": False, "error": res.stderr, "output": res.stdout}

        data: Any = res.stdout
        if output == "json":
            try:
                data = json.loads(res.stdout)
            except json.JSONDecodeError:
                pass
        return {"success": True, "data": data, "output": res.stdout, "error": ""}

    def describe(
        self,
        resource: str,
        name: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        cmd = ["kubectl", "describe", resource, name]
        if namespace:
            cmd.extend(["-n", namespace])

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
        }

    def delete_pod(
        self,
        name: str,
        namespace: str = "default",
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd = ["kubectl", "delete", "pod", name, "-n", namespace]
        if dry_run:
            cmd.append("--dry-run=server")

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }

    def delete_pods_by_phase(
        self,
        phase: str,
        *,
        all_namespaces: bool = True,
        field_selector: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        selectors = []
        if phase:
            selectors.append(f"status.phase={phase}")
        if field_selector:
            selectors.append(field_selector)

        cmd = ["kubectl", "delete", "pods"]
        if all_namespaces:
            cmd.append("--all-namespaces")
        if selectors:
            cmd.append(f"--field-selector={','.join(selectors)}")
        if dry_run:
            cmd.append("--dry-run=server")

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }

    def rollout_restart(
        self,
        kind: str,
        name: str,
        namespace: str = "default",
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd = ["kubectl", "rollout", "restart", f"{kind}/{name}", "-n", namespace]
        if dry_run:
            # Check rollout status/spec instead of executing restart mutation
            cmd = ["kubectl", "rollout", "status", f"{kind}/{name}", "-n", namespace]

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }

    def wait_rollout(
        self,
        kind: str,
        name: str,
        namespace: str = "default",
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        cmd = [
            "kubectl",
            "rollout",
            "status",
            f"{kind}/{name}",
            "-n",
            namespace,
            f"--timeout={timeout_seconds}s",
        ]
        res = self._runner.run(cmd, timeout_seconds=timeout_seconds + 5)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
        }

    def wait_workload_ready(
        self,
        workload: str,
        namespace: str = "default",
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Check whether pods for a given workload have reached Running state."""
        cmd = [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={workload}",
            "-o",
            "json",
        ]
        res = self._runner.run(cmd, timeout_seconds=timeout_seconds)
        if not res.success:
            return {"success": False, "error": res.stderr, "output": res.stdout}

        try:
            data = json.loads(res.stdout)
            items = data.get("items", [])
            if not items:
                # Fall back to substring match on pod names
                cmd_all = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
                res_all = self._runner.run(cmd_all)
                data_all = json.loads(res_all.stdout)
                items = [
                    p for p in data_all.get("items", [])
                    if workload in p.get("metadata", {}).get("name", "")
                ]

            if not items:
                return {
                    "success": False,
                    "error": f"No pods found for workload {workload}",
                    "output": "",
                }

            all_running = all(
                p.get("status", {}).get("phase") == "Running"
                and any(
                    cond.get("type") == "Ready" and cond.get("status") == "True"
                    for cond in p.get("status", {}).get("conditions", [])
                )
                for p in items
            )
            return {
                "success": all_running,
                "output": f"{len(items)} pod(s) checked, ready={all_running}",
                "error": "" if all_running else "One or more pods are not yet Ready",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "output": res.stdout}

    def apply(
        self,
        filepath: str | None = None,
        content: str | None = None,
        namespace: str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not filepath and not content:
            return {"success": False, "error": "Either file or content required", "output": ""}

        cmd = ["kubectl", "apply"]
        if filepath:
            cmd.extend(["-f", filepath])
        elif content:
            cmd.extend(["-f", "-"])

        if namespace:
            cmd.extend(["-n", namespace])
        if dry_run:
            cmd.append("--dry-run=server")

        stdin_input = content if (content and not filepath) else None
        res = self._runner.run(cmd, input=stdin_input)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }

    def logs(
        self,
        name: str,
        namespace: str = "default",
        container: str | None = None,
        tail: int = 100,
    ) -> dict[str, Any]:
        cmd = ["kubectl", "logs", name, "-n", namespace, f"--tail={tail}"]
        if container:
            cmd.extend(["-c", container])

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
        }


default_kubectl = KubectlTool()
