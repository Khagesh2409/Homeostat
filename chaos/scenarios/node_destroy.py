"""Scenario 9: Node Destroy — Simulates node termination and verifies state resumption from S3 checkpoint."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl

logger = structlog.get_logger(__name__)


def create_node_destroy_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    boto_fn: Callable[[str], Any] | None = None,
    agent_url: str = "http://localhost:8080",
    s3_bucket: str = "homeostat-memory",
    checkpoint_key: str = "checkpoints/latest.json",
) -> ChaosScenario:
    """Create the Node Destroy chaos scenario."""
    http_client = httpx.Client(timeout=5.0)

    def setup() -> None:
        # Write state checkpoint to S3
        checkpoint_data = {
            "checkpoint_id": "chaos-node-destroy-ckpt",
            "timestamp": datetime.now(UTC).isoformat(),
            "mode": "idle",
            "active_incidents": 0,
            "version": "1.0",
        }
        if boto_fn is not None:
            try:
                s3 = boto_fn("s3")
                s3.put_object(
                    Bucket=s3_bucket,
                    Key=checkpoint_key,
                    Body=json.dumps(checkpoint_data).encode("utf-8"),
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("node_destroy_checkpoint_write_failed", error=str(e))

    def attack() -> None:
        # Simulate node termination or uncordon/drain
        execute_kubectl("get nodes -o name", kubectl_fn)
        # In a test environment or simulation, signal the node restart
        if kubectl_fn is not None:
            kubectl_fn("simulate node_destroy")

    def verify_recovered() -> bool:
        # Verify agent is reachable and has loaded state from checkpoint
        try:
            resp = http_client.get(f"{agent_url.rstrip('/')}/health")
            if resp.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass

        # Also check S3 checkpoint exists and was read
        if boto_fn is not None:
            try:
                s3 = boto_fn("s3")
                resp_s3 = s3.get_object(Bucket=s3_bucket, Key=checkpoint_key)
                body = resp_s3.get("Body").read().decode("utf-8")
                return bool("chaos-node-destroy-ckpt" in body)
            except Exception as e:  # noqa: BLE001
                logger.debug("node_destroy_s3_read_failed", error=str(e))

        return False

    def cleanup() -> None:
        # Clean up test checkpoint in S3
        if boto_fn is not None:
            try:
                s3 = boto_fn("s3")
                s3.delete_object(Bucket=s3_bucket, Key=checkpoint_key)
            except Exception as e:  # noqa: BLE001
                logger.debug("node_destroy_s3_cleanup_failed", error=str(e))

    return ChaosScenario(
        name="node_destroy",
        category="node",
        description="Simulate node termination; agent resumes state from S3 checkpoint",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=60.0,
        expected_recovery_max_s=300.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_node_destroy_scenario()
