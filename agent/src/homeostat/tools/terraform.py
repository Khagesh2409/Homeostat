"""
Terraform tool wrapper.

Supports plan, apply, and destroy operations.
Dry-run constraint: apply and destroy run 'terraform plan' only when dry_run=True.
"""

from __future__ import annotations

import logging
from typing import Any

from homeostat.tools.runner import CommandRunner, default_runner

logger = logging.getLogger(__name__)


class TerraformTool:
    """Interface to Terraform CLI."""

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
        working_dir = args.get("working_dir", args.get("dir", "."))
        target = args.get("target")

        if cmd_name == "plan":
            return self.plan(working_dir=working_dir, target=target)
        elif cmd_name == "apply":
            return self.apply(working_dir=working_dir, target=target, dry_run=dry_run)
        elif cmd_name == "destroy":
            return self.destroy(working_dir=working_dir, target=target, dry_run=dry_run)
        else:
            return {
                "success": False,
                "error": f"Unsupported terraform command: {command}",
                "output": "",
            }

    def plan(
        self,
        working_dir: str = ".",
        target: str | None = None,
    ) -> dict[str, Any]:
        cmd = ["terraform", f"-chdir={working_dir}", "plan", "-input=false", "-no-color"]
        if target:
            cmd.extend(["-target", target])

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": True,
        }

    def apply(
        self,
        working_dir: str = ".",
        target: str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            logger.info("Terraform apply dry-run: running plan instead")
            return self.plan(working_dir=working_dir, target=target)

        cmd = [
            "terraform",
            f"-chdir={working_dir}",
            "apply",
            "-auto-approve",
            "-input=false",
            "-no-color",
        ]
        if target:
            cmd.extend(["-target", target])

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }

    def destroy(
        self,
        working_dir: str = ".",
        target: str | None = None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            logger.info("Terraform destroy dry-run: running plan -destroy instead")
            cmd = [
                "terraform",
                f"-chdir={working_dir}",
                "plan",
                "-destroy",
                "-input=false",
                "-no-color",
            ]
            if target:
                cmd.extend(["-target", target])
            res = self._runner.run(cmd)
            return {
                "success": res.success,
                "output": res.stdout,
                "error": res.stderr if not res.success else "",
                "dry_run": True,
            }

        cmd = [
            "terraform",
            f"-chdir={working_dir}",
            "destroy",
            "-auto-approve",
            "-input=false",
            "-no-color",
        ]
        if target:
            cmd.extend(["-target", target])

        res = self._runner.run(cmd)
        return {
            "success": res.success,
            "output": res.stdout,
            "error": res.stderr if not res.success else "",
            "dry_run": dry_run,
        }


default_terraform = TerraformTool()
