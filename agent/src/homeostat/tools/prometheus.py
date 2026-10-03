"""
Prometheus tool wrapper.

Supports querying metrics, checking active alerts, and verifying target health
via the Prometheus HTTP API. Read-only by nature.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from homeostat.config import settings

logger = logging.getLogger(__name__)


class PrometheusTool:
    """Interface to Prometheus HTTP API."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.prometheus_url).rstrip("/")

    def run_command(
        self,
        command: str,
        args: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cmd_name = command.lower()
        if cmd_name in ("query", "query_metrics"):
            return self.query_metrics(
                query=str(args.get("query", "")),
                time=args.get("time"),
            )
        elif cmd_name in ("alerts", "check_alert_status"):
            return self.check_alert_status(alertname=args.get("alertname"))
        elif cmd_name == "check_target_up":
            return self.check_target_up(
                job=str(args.get("job", "")),
                namespace=args.get("namespace"),
            )
        else:
            return {
                "success": False,
                "error": f"Unsupported prometheus command: {command}",
                "output": "",
            }

    def query_metrics(
        self,
        query: str,
        time: str | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Query Prometheus instant evaluation endpoint."""
        url = f"{self._base_url}/api/v1/query"
        params = {"query": query}
        if time:
            params["time"] = time

        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            if data.get("status") == "success":
                result = data.get("data", {}).get("result", [])
                return {
                    "success": True,
                    "output": f"Found {len(result)} metric results",
                    "data": result,
                    "error": "",
                }
            return {
                "success": False,
                "error": f"Prometheus query status: {data.get('status')}",
                "output": "",
            }
        except Exception as exc:
            logger.warning("Prometheus query failed for '%s': %s", query, exc)
            return {
                "success": False,
                "error": f"Prometheus query failed: {exc}",
                "output": "",
            }

    def check_alert_status(
        self,
        alertname: str | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Check active alerts currently firing in Prometheus."""
        url = f"{self._base_url}/api/v1/alerts"
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()

            alerts = data.get("data", {}).get("alerts", [])
            if alertname:
                alerts = [
                    a for a in alerts
                    if a.get("labels", {}).get("alertname") == alertname
                ]

            is_firing = any(a.get("state") == "firing" for a in alerts)
            return {
                "success": True,
                "output": f"{len(alerts)} alerts matching (firing={is_firing})",
                "firing": is_firing,
                "alerts": alerts,
                "error": "",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to check Prometheus alerts: {exc}",
                "output": "",
            }

    def check_target_up(
        self,
        job: str,
        namespace: str | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Verify if a scrape target is reporting UP."""
        url = f"{self._base_url}/api/v1/targets"
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()

            active = data.get("data", {}).get("activeTargets", [])
            matches = [
                t for t in active
                if t.get("labels", {}).get("job") == job
                or t.get("discoveredLabels", {}).get("__meta_kubernetes_service_name") == job
            ]

            if namespace:
                matches = [
                    t for t in matches
                    if t.get("labels", {}).get("namespace") == namespace
                    or t.get("discoveredLabels", {}).get("__meta_kubernetes_namespace") == namespace
                ]

            if not matches:
                # If no direct targets match, query up metric as fallback
                query_res = self.query_metrics(f'up{{job="{job}"}}')
                if query_res["success"] and query_res.get("data"):
                    up_val = query_res["data"][0].get("value", [0, "0"])[1]
                    is_up = up_val == "1"
                    return {
                        "success": is_up,
                        "output": f"Target {job} metric up={up_val}",
                        "error": "" if is_up else f"Target {job} is down (metric up={up_val})",
                    }
                return {
                    "success": False,
                    "error": f"No scrape target found for job='{job}'",
                    "output": "",
                }

            all_healthy = all(t.get("health") == "up" for t in matches)
            return {
                "success": all_healthy,
                "output": f"{len(matches)} target(s) for job={job}, all_up={all_healthy}",
                "error": "" if all_healthy else f"Target {job} is not healthy",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Prometheus target check failed: {exc}",
                "output": "",
            }


default_prometheus = PrometheusTool()
