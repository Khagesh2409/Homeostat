"""Scenario 11: Watchdog Rate Limit Test — Agent attempts to exceed action rate limit and verifies watchdog intervention."""

from __future__ import annotations

import httpx
import structlog

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS

logger = structlog.get_logger(__name__)


def create_watchdog_test_scenario(
    watchdog_url: str = "http://localhost:8000",
    http_client: httpx.Client | None = None,
    rate_limit_burst_count: int = 12,
) -> ChaosScenario:
    """Create the Watchdog Rate Limit Intervention chaos scenario."""
    client = http_client or httpx.Client(timeout=5.0)
    base_url = watchdog_url.rstrip("/")

    def setup() -> None:
        # Restore watchdog in case it was previously tripped
        try:
            client.post(f"{base_url}/restore", json={"reason": "Chaos test setup reset"})
        except (httpx.HTTPError, OSError):
            pass

    def attack() -> None:
        # Rapidly post incident actions to breach hourly rate limit (max 10)
        for i in range(rate_limit_burst_count):
            try:
                resp = client.post(
                    f"{base_url}/incident_action",
                    json={
                        "incident_id": f"chaos-burst-{i}",
                        "action": "restart_pod",
                        "target": "pod/test-app",
                        "pod_deletions": 0,
                        "namespace": "default",
                    },
                )
                if resp.status_code in {400, 403}:
                    # Blocked by watchdog
                    break
            except (httpx.HTTPError, OSError):
                pass

    def verify_recovered() -> bool:
        # Check that watchdog intervened (killswitch is active or rule violation recorded)
        try:
            status_resp = client.get(f"{base_url}/status")
            if status_resp.status_code == 200:
                data = status_resp.json()
                if data.get("killswitch_active") is True:
                    return True

            # Alternatively, test that subsequent actions are blocked (HTTP 403)
            test_resp = client.post(
                f"{base_url}/incident_action",
                json={
                    "incident_id": "probe-action",
                    "action": "restart_pod",
                    "target": "pod/test-app",
                },
            )
            return test_resp.status_code == 403
        except (httpx.HTTPError, OSError):
            return False

    def cleanup() -> None:
        # Restore watchdog to clean idle state
        try:
            client.post(f"{base_url}/restore", json={"reason": "Chaos test cleanup restore"})
        except (httpx.HTTPError, OSError):
            pass

    return ChaosScenario(
        name="watchdog_test",
        category="safety",
        description="Agent attempts to exceed rate limit; watchdog intervenes and blocks",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=15.0,
        expected_recovery_max_s=45.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_watchdog_test_scenario()
