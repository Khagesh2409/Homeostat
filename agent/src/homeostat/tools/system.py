"""
System diagnostics and host maintenance tool.

Supports disk usage checks, process list, network reachability, DNS lookup,
and container image pruning.
"""

from __future__ import annotations

import logging
import shutil
import socket
from typing import Any

from homeostat.tools.runner import CommandRunner, default_runner

logger = logging.getLogger(__name__)


class SystemTool:
    """Interface to node-level and OS-level system diagnostics."""

    def __init__(self, runner: CommandRunner = default_runner) -> None:
        self._runner = runner

    def run_command(
        self,
        command: str,
        args: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd_name = command.lower()
        if cmd_name in ("disk_usage", "check_disk_usage"):
            return self.check_disk_usage(
                path=str(args.get("path", "/")),
                max_percent=float(args.get("max_percent", 80.0)),
                node=args.get("node"),
            )
        elif cmd_name == "process_list":
            return self.process_list(filter_name=args.get("filter"))
        elif cmd_name == "network_check":
            return self.network_check(
                host=str(args.get("host", args.get("target", "127.0.0.1"))),
                port=int(args.get("port", 80)),
                timeout_seconds=int(args.get("timeout_seconds", 5)),
            )
        elif cmd_name == "dns_lookup":
            return self.dns_lookup(hostname=str(args.get("hostname", args.get("host", ""))))
        elif cmd_name == "prune_images":
            return self.prune_images(node=args.get("node"), dry_run=dry_run)
        else:
            return {
                "success": False,
                "error": f"Unsupported system command: {command}",
                "output": "",
            }

    def check_disk_usage(
        self,
        path: str = "/",
        max_percent: float = 80.0,
        node: str | None = None,
    ) -> dict[str, Any]:
        """Check disk space usage percentage."""
        try:
            total, used, free = shutil.disk_usage(path)
            used_pct = (used / total) * 100.0 if total > 0 else 0.0
            is_ok = used_pct <= max_percent
            node_str = f" on node {node}" if node else ""
            msg = (
                f"Disk usage{node_str} for {path}: {used_pct:.1f}% "
                f"(used: {used / (1024**3):.1f}GB / {total / (1024**3):.1f}GB, "
                f"threshold: {max_percent}%)"
            )
            return {
                "success": is_ok,
                "output": msg,
                "error": "" if is_ok else f"Disk usage {used_pct:.1f}% exceeds {max_percent}%",
                "used_percent": used_pct,
                "total_bytes": total,
                "free_bytes": free,
            }
        except Exception as exc:
            # Fall back to df command
            res = self._runner.run(["df", "-h", path])
            return {
                "success": res.success,
                "output": res.stdout,
                "error": res.stderr if not res.success else str(exc),
            }

    def process_list(self, filter_name: str | None = None) -> dict[str, Any]:
        """List running processes."""
        cmd = ["ps", "aux"]
        res = self._runner.run(cmd)
        if not res.success:
            return {"success": False, "error": res.stderr, "output": res.stdout}

        lines = res.stdout.splitlines()
        if filter_name:
            header = lines[:1]
            matches = [line for line in lines[1:] if filter_name in line]
            filtered_output = "\n".join(header + matches)
            return {
                "success": True,
                "output": filtered_output,
                "count": len(matches),
                "error": "",
            }

        return {
            "success": True,
            "output": res.stdout[:2000],  # bounded output
            "count": len(lines) - 1,
            "error": "",
        }

    def network_check(
        self,
        host: str,
        port: int = 80,
        timeout_seconds: int = 5,
    ) -> dict[str, Any]:
        """Test TCP connection reachability."""
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return {
                    "success": True,
                    "output": f"Successfully connected to {host}:{port}",
                    "error": "",
                }
        except Exception as exc:
            return {
                "success": False,
                "output": "",
                "error": f"Failed to connect to {host}:{port}: {exc}",
            }

    def dns_lookup(self, hostname: str) -> dict[str, Any]:
        """Resolve a hostname to IP addresses."""
        if not hostname:
            return {"success": False, "error": "Hostname is required", "output": ""}
        try:
            info = socket.getaddrinfo(hostname, None)
            ips = sorted({str(entry[4][0]) for entry in info})
            return {
                "success": True,
                "output": f"Resolved {hostname} to: {', '.join(ips)}",
                "ips": ips,
                "error": "",
            }
        except socket.gaierror as exc:
            return {
                "success": False,
                "error": f"DNS resolution failed for {hostname}: {exc}",
                "output": "",
            }

    def prune_images(
        self,
        node: str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Prune unused container images on node."""
        if dry_run:
            target_node = node or "current node"
            return {
                "success": True,
                "output": f"[dry-run] Would prune unused container images on {target_node}",
                "error": "",
                "dry_run": True,
            }

        # Try crictl (Kubernetes node standard), fallback to docker
        res = self._runner.run(["crictl", "rmi", "--prune"])
        if not res.success:
            res = self._runner.run(["docker", "image", "prune", "-f"])

        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }


default_system = SystemTool()
