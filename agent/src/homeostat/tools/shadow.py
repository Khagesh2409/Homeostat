"""Shadow environment management tool for testing candidate changes safely."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeostat.tools.kubectl import KubectlTool, default_kubectl

logger = logging.getLogger(__name__)

DEFAULT_SHADOW_NAMESPACE = "homeostat-shadow"


class ShadowTool:
    """Interface to manage the isolated shadow namespace and rehearsal workloads."""

    def __init__(self, kubectl: KubectlTool = default_kubectl) -> None:
        self._kubectl = kubectl

    def run_command(
        self,
        command: str,
        args: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Dispatch shadow environment operations."""
        cmd_name = command.lower()
        if cmd_name in ("clone_workload", "clone"):
            src_ns = str(args.get("source_namespace", args.get("namespace", "default")))
            return self.clone_workload(
                source_workload=str(args.get("source_workload", args.get("workload", ""))),
                source_namespace=src_ns,
                shadow_namespace=str(args.get("shadow_namespace", DEFAULT_SHADOW_NAMESPACE)),
                scale=int(args.get("scale", 1)),
                dry_run=dry_run,
            )
        elif cmd_name in ("apply_candidate_change", "apply_change", "apply"):
            return self.apply_candidate_change(
                workload=str(args.get("workload", "")),
                patch_data=args.get("patch_data", args.get("content", {})),
                namespace=str(args.get("namespace", DEFAULT_SHADOW_NAMESPACE)),
                dry_run=dry_run,
            )
        elif cmd_name in ("check_shadow_health", "check_health", "verify"):
            return self.check_shadow_health(
                workload=str(args.get("workload", "")),
                namespace=str(args.get("namespace", DEFAULT_SHADOW_NAMESPACE)),
                timeout_seconds=int(args.get("timeout_seconds", 30)),
            )
        elif cmd_name in ("cleanup_shadow", "cleanup", "delete"):
            return self.cleanup_shadow(
                workload=str(args.get("workload", "")),
                namespace=str(args.get("namespace", DEFAULT_SHADOW_NAMESPACE)),
                dry_run=dry_run,
            )

        return {
            "success": False,
            "error": f"Unknown shadow command: '{command}'",
            "output": "",
        }

    def clone_workload(
        self,
        source_workload: str,
        source_namespace: str = "default",
        shadow_namespace: str = DEFAULT_SHADOW_NAMESPACE,
        scale: int = 1,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Fetch a production deployment, scrub cluster metadata, and clone into shadow."""
        logger.info(
            "Cloning workload '%s' from '%s' to '%s' (scale=%d)",
            source_workload,
            source_namespace,
            shadow_namespace,
            scale,
        )

        get_res = self._kubectl.get(
            resource="deployment",
            name=source_workload,
            namespace=source_namespace,
            output="json",
        )
        if not get_res.get("success"):
            return {
                "success": False,
                "error": f"Failed to fetch source deployment '{source_workload}': "
                f"{get_res.get('error')}",
                "output": get_res.get("output", ""),
            }

        try:
            dep_data = json.loads(str(get_res.get("output", "{}")))
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to parse source deployment JSON: {exc}",
                "output": "",
            }

        # Scrub cluster-specific metadata and isolate into shadow namespace
        metadata = dep_data.get("metadata", {})
        for key in (
            "resourceVersion",
            "uid",
            "creationTimestamp",
            "generation",
            "managedFields",
            "selfLink",
        ):
            metadata.pop(key, None)

        metadata["namespace"] = shadow_namespace
        labels = metadata.get("labels", {})
        labels["homeostat.io/environment"] = "shadow"
        metadata["labels"] = labels
        dep_data["metadata"] = metadata

        spec = dep_data.get("spec", {})
        spec["replicas"] = scale
        dep_data["spec"] = spec
        dep_data.pop("status", None)

        content = json.dumps(dep_data)
        apply_res = self._kubectl.apply(
            content=content,
            namespace=shadow_namespace,
            dry_run=dry_run,
        )

        return {
            "success": bool(apply_res.get("success", False)),
            "output": apply_res.get("output", ""),
            "error": apply_res.get("error", ""),
            "shadow_workload": source_workload,
            "shadow_namespace": shadow_namespace,
            "cloned_manifest": dep_data,
        }

    def apply_candidate_change(
        self,
        workload: str,
        patch_data: dict[str, Any] | str,
        namespace: str = DEFAULT_SHADOW_NAMESPACE,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply a candidate modification or patch to the shadow deployment."""
        content = (
            patch_data
            if isinstance(patch_data, str)
            else json.dumps(patch_data)
        )
        return self._kubectl.apply(
            content=content,
            namespace=namespace,
            dry_run=dry_run,
        )

    def check_shadow_health(
        self,
        workload: str,
        namespace: str = DEFAULT_SHADOW_NAMESPACE,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        """Verify that the shadow workload is healthy and running."""
        ready_res = self._kubectl.wait_workload_ready(
            workload=workload,
            namespace=namespace,
            timeout_seconds=timeout_seconds,
        )
        is_healthy = bool(ready_res.get("success", False))

        return {
            "success": is_healthy,
            "healthy": is_healthy,
            "workload": workload,
            "namespace": namespace,
            "output": ready_res.get("output", ""),
            "error": ready_res.get("error", ""),
        }

    def cleanup_shadow(
        self,
        workload: str,
        namespace: str = DEFAULT_SHADOW_NAMESPACE,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Clean up the shadow workload to conserve node resources."""
        if not workload:
            return {"success": True, "output": "No workload specified for cleanup"}

        res = self._kubectl.run_command(
            "delete",
            {
                "resource": "deployment",
                "name": workload,
                "namespace": namespace,
            },
            dry_run=dry_run,
        )
        return {
            "success": bool(res.get("success", False)),
            "output": res.get("output", ""),
            "error": res.get("error", ""),
        }


default_shadow = ShadowTool()
