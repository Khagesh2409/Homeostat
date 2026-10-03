"""
Helm tool wrapper.

Supports upgrade and rollback operations with `--dry-run` support.
"""

from __future__ import annotations

import logging
from typing import Any

from homeostat.tools.runner import CommandRunner, default_runner

logger = logging.getLogger(__name__)


class HelmTool:
    """Interface to Helm CLI."""

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
        if cmd_name == "upgrade":
            return self.upgrade(
                release=str(args.get("release", args.get("name", ""))),
                chart=str(args.get("chart", "")),
                namespace=str(args.get("namespace", "default")),
                values=args.get("values"),
                dry_run=dry_run,
            )
        elif cmd_name == "rollback":
            return self.rollback(
                release=str(args.get("release", args.get("name", ""))),
                revision=args.get("revision"),
                namespace=str(args.get("namespace", "default")),
                dry_run=dry_run,
            )
        else:
            return {
                "success": False,
                "error": f"Unsupported helm command: {command}",
                "output": "",
            }

    def upgrade(
        self,
        release: str,
        chart: str,
        namespace: str = "default",
        values: dict[str, Any] | str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd = ["helm", "upgrade", release, chart, "-n", namespace]
        if dry_run:
            cmd.append("--dry-run")
        if isinstance(values, str):
            cmd.extend(["-f", values])

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }

    def rollback(
        self,
        release: str,
        revision: int | str | None = None,
        namespace: str = "default",
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd = ["helm", "rollback", release]
        if revision is not None:
            cmd.append(str(revision))
        cmd.extend(["-n", namespace])
        if dry_run:
            cmd.append("--dry-run")

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }


default_helm = HelmTool()
