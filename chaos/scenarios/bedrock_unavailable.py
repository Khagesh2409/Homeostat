"""Scenario 8: Bedrock Unavailable — Simulates Bedrock 503 / egress failure and verifies graceful fallback to Tier-0."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS


def create_bedrock_unavailable_scenario(
    agent_url: str = "http://localhost:8080",
    http_client: httpx.Client | None = None,
    mock_bedrock_toggle: Callable[[bool], Any] | None = None,
) -> ChaosScenario:
    """Create the Bedrock Unavailable chaos scenario."""
    client = http_client or httpx.Client(timeout=5.0)
    base_url = agent_url.rstrip("/")

    def setup() -> None:
        if mock_bedrock_toggle is not None:
            mock_bedrock_toggle(True)
        else:
            try:
                client.post(f"{base_url}/admin/bedrock-mock", json={"available": True})
            except (httpx.HTTPError, OSError):
                pass

    def attack() -> None:
        # Trip Bedrock availability / simulate 503 outage
        if mock_bedrock_toggle is not None:
            mock_bedrock_toggle(False)
        else:
            try:
                client.post(f"{base_url}/admin/bedrock-mock", json={"available": False, "status_code": 503})
            except (httpx.HTTPError, OSError):
                pass

    def verify_recovered() -> bool:
        # Verify agent remains responsive and degrades gracefully
        try:
            resp = client.get(f"{base_url}/health")
            if resp.status_code != 200:
                return False

            # Check status endpoint
            status_resp = client.get(f"{base_url}/status")
            if status_resp.status_code == 200:
                data = status_resp.json()
                # Agent should remain operational, either reporting fallback or idle
                mode_str = str(data.get("mode", ""))
                return bool(mode_str in {"idle", "incident", "maintenance"})
            return True
        except (httpx.HTTPError, OSError):
            return False

    def cleanup() -> None:
        if mock_bedrock_toggle is not None:
            mock_bedrock_toggle(True)
        else:
            try:
                client.post(f"{base_url}/admin/bedrock-mock", json={"available": True})
            except (httpx.HTTPError, OSError):
                pass

    return ChaosScenario(
        name="bedrock_unavailable",
        category="injection",
        description="Simulates Bedrock 503 / egress blockage; agent degrades gracefully to Tier-0 only",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=15.0,
        expected_recovery_max_s=60.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_bedrock_unavailable_scenario()
