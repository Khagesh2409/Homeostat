"""
Memory tool wrapper for reading and writing incident runbooks to DynamoDB.
"""

from __future__ import annotations

import logging
from typing import Any

from homeostat.memory.client import RunbookClient

logger = logging.getLogger(__name__)


class MemoryTool:
    """Interface to DynamoDB runbook memory store."""

    def __init__(self, client: RunbookClient | None = None) -> None:
        self._client = client

    def _get_client(self) -> RunbookClient:
        if self._client is None:
            self._client = RunbookClient()
        return self._client

    def run_command(
        self,
        command: str,
        args: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd_name = command.lower()
        if cmd_name in ("get", "read", "read_runbook"):
            return self.read_runbook(
                failure_signature=str(args.get("failure_signature", args.get("key", ""))),
            )
        elif cmd_name in ("put", "write", "write_runbook"):
            return self.write_runbook(
                failure_signature=str(args.get("failure_signature", "")),
                diagnosis=str(args.get("diagnosis", "")),
                plan_steps=args.get("plan_steps", args.get("steps", [])),
                rationale=str(args.get("rationale", "")),
                dry_run=dry_run,
            )
        else:
            return {
                "success": False,
                "error": f"Unsupported memory command: {command}",
                "output": "",
            }

    def read_runbook(self, failure_signature: str) -> dict[str, Any]:
        try:
            client = self._get_client()
            runbook = client.get_latest_runbook(failure_signature)
            if runbook:
                return {
                    "success": True,
                    "output": f"Found runbook for {failure_signature}",
                    "data": runbook,
                    "error": "",
                }
            return {
                "success": False,
                "output": "",
                "error": f"No runbook found for {failure_signature}",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Memory lookup failed: {exc}",
                "output": "",
            }

    def write_runbook(
        self,
        failure_signature: str,
        diagnosis: str,
        plan_steps: list[dict[str, Any]],
        rationale: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            return {
                "success": True,
                "output": f"[dry-run] Would save runbook for {failure_signature}",
                "error": "",
                "dry_run": True,
            }
        try:
            client = self._get_client()
            success = client.save_runbook(
                failure_signature=failure_signature,
                diagnosis=diagnosis,
                plan_steps=plan_steps,
                rationale=rationale,
            )
            return {
                "success": success,
                "output": f"Saved runbook for {failure_signature}" if success else "",
                "error": "" if success else "Failed to write runbook to DynamoDB",
                "dry_run": False,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Memory write failed: {exc}",
                "output": "",
            }


default_memory = MemoryTool()
