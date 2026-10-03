"""
Unit and integration tests for FastAPI HTTP server, alert intake, and Prometheus metrics.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from homeostat.intake import (
    AlertDeduplicator,
    AlertQueue,
    parse_alertmanager_alert,
    parse_alertmanager_payload,
    parse_k8s_event,
)
from homeostat.metrics import (
    get_metrics_output,
    record_incident,
    record_llm_usage,
    record_runbook_retrieval,
    record_tier0_action,
    set_budget_remaining,
)
from homeostat.server import create_app
from homeostat.state import AgentState, Alert, AlertSeverity

# ── 1. Prometheus Metrics Tests ───────────────────────────────


def test_metrics_recording() -> None:
    record_incident("critical", "tier0", duration_seconds=12.5)
    record_llm_usage(
        model="claude-3-haiku", input_tokens=100, output_tokens=50, cost_usd=0.0005
    )
    record_runbook_retrieval("hit")
    record_runbook_retrieval("wrong")
    record_tier0_action("dns-restart", "success")
    set_budget_remaining(0.45)

    output = get_metrics_output().decode("utf-8")
    assert "homeostat_incidents_total" in output
    assert "homeostat_recovery_duration_seconds" in output
    assert "homeostat_llm_tokens_total" in output
    assert "homeostat_llm_cost_usd" in output
    assert "homeostat_runbook_retrievals_total" in output
    assert "homeostat_tier0_actions_total" in output
    assert "homeostat_budget_remaining_usd" in output


# ── 2. Alert Intake Parsing Tests ─────────────────────────────


def test_parse_alertmanager_alert() -> None:
    raw_item = {
        "status": "firing",
        "labels": {
            "alertname": "CrashLoopBackOff",
            "severity": "critical",
            "namespace": "prod",
            "pod": "api-6789-xyz",
        },
        "annotations": {
            "summary": "Container crashing",
            "message": "CrashLoopBackOff on container web",
        },
    }
    alert = parse_alertmanager_alert(raw_item)
    assert alert.alertname == "CrashLoopBackOff"
    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.namespace == "prod"
    assert alert.source == "pod/api-6789-xyz"
    assert "CrashLoopBackOff on container web" in alert.message


def test_parse_alertmanager_payload_filters_resolved() -> None:
    payload = {
        "alerts": [
            {
                "status": "resolved",
                "labels": {"alertname": "OldResolvedAlert", "severity": "info"},
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "ActiveAlert",
                    "severity": "warning",
                    "instance": "node-1",
                },
            },
        ]
    }
    alerts = parse_alertmanager_payload(payload)
    assert len(alerts) == 1
    assert alerts[0].alertname == "ActiveAlert"
    assert alerts[0].source == "node/node-1"


def test_parse_k8s_event_payload() -> None:
    k8s_payload = {
        "reason": "FailedScheduling",
        "type": "Warning",
        "namespace": "default",
        "resource_kind": "pod",
        "resource_name": "worker-1",
        "message": "0/3 nodes are available: insufficient memory",
    }
    alert = parse_k8s_event(k8s_payload)
    assert alert.alertname == "FailedScheduling"
    assert alert.severity == AlertSeverity.WARNING
    assert alert.source == "pod/worker-1"
    assert "insufficient memory" in alert.message


# ── 3. Alert Deduplication Tests ──────────────────────────────


def test_alert_deduplicator() -> None:
    dedup = AlertDeduplicator(window_seconds=10.0)
    alert = Alert(
        alertname="OOMKilled",
        severity=AlertSeverity.CRITICAL,
        namespace="default",
        source="pod/db-0",
        message="",
    )

    t0 = 1000.0
    # First time -> not duplicate
    assert dedup.is_duplicate(alert, now=t0) is False

    # Within window -> duplicate
    assert dedup.is_duplicate(alert, now=t0 + 5.0) is True

    # After window -> allowed again
    assert dedup.is_duplicate(alert, now=t0 + 11.0) is False


# ── 4. Alert Queue & Processing Tests ─────────────────────────


@pytest.mark.asyncio
async def test_alert_queue_processing() -> None:
    executed_states: list[AgentState] = []

    def mock_executor(state: AgentState) -> dict[str, Any]:
        executed_states.append(state)
        return {"outcome": "RESOLVED", "is_tier0": True}

    queue = AlertQueue(graph_executor=mock_executor)
    alert = Alert(
        alertname="DiskPressure",
        severity=AlertSeverity.CRITICAL,
        namespace="default",
        source="node/worker-2",
        message="Disk almost full",
    )

    assert queue.enqueue(alert) is True
    # Duplicate immediately dropped
    assert queue.enqueue(alert) is False

    status_dict = queue.get_status()
    assert status_dict["total_received"] == 2
    assert status_dict["total_enqueued"] == 1
    assert status_dict["total_duplicates"] == 1

    # Process one alert
    popped = await queue.queue.get()
    result = await queue.process_one(popped)

    assert result["outcome"] == "RESOLVED"
    assert len(executed_states) == 1
    assert executed_states[0]["current_alert"].alertname == "DiskPressure"
    assert queue.total_processed == 1


# ── 5. FastAPI Server Endpoint Tests ──────────────────────────


@pytest.fixture
def mock_app() -> tuple[Any, AlertQueue, list[AgentState]]:
    captured: list[AgentState] = []

    def executor(state: AgentState) -> dict[str, Any]:
        captured.append(state)
        return {"outcome": "RESOLVED"}

    test_queue = AlertQueue(graph_executor=executor)
    test_app = create_app(alert_queue=test_queue)
    return test_app, test_queue, captured


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints(
    mock_app: tuple[Any, AlertQueue, list[AgentState]],
) -> None:
    app, queue, _ = mock_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # GET /health
        res_health = await client.get("/health")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "ok"
        assert res_health.json()["service"] == "homeostat"

        # GET /ready (when worker is running or not)
        queue._running = True
        res_ready = await client.get("/ready")
        assert res_ready.status_code == 200
        assert res_ready.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_metrics_endpoint(mock_app: tuple[Any, AlertQueue, list[AgentState]]) -> None:
    app, _, _ = mock_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/metrics")
        assert res.status_code == 200
        assert "text/plain" in res.headers["content-type"]
        assert "homeostat_" in res.text


@pytest.mark.asyncio
async def test_status_endpoint(mock_app: tuple[Any, AlertQueue, list[AgentState]]) -> None:
    app, _, _ = mock_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/status")
        assert res.status_code == 200
        data = res.json()
        assert data["service"] == "homeostat"
        assert "mode" in data
        assert "queue" in data


@pytest.mark.asyncio
async def test_watchdog_ack_endpoint(mock_app: tuple[Any, AlertQueue, list[AgentState]]) -> None:
    app, _, _ = mock_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/watchdog/ack", json={"command": "heartbeat"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "acknowledged"
        assert data["healthy"] is True


@pytest.mark.asyncio
async def test_post_alerts_alertmanager_webhook(
    mock_app: tuple[Any, AlertQueue, list[AgentState]]
) -> None:
    app, queue, _ = mock_app
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "shop",
                    "pod": "frontend-xyz",
                },
                "annotations": {"summary": "Frontend container in CrashLoopBackOff"},
            },
            {
                "status": "resolved",
                "labels": {"alertname": "OldAlert", "severity": "info"},
            },
        ],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/alerts", json=payload)
        assert res.status_code == 202
        data = res.json()
        assert data["status"] == "accepted"
        assert data["received"] == 1
        assert data["enqueued"] == 1
        assert data["duplicates"] == 0
        assert queue.queue.qsize() == 1


@pytest.mark.asyncio
async def test_post_events_k8s_webhook(
    mock_app: tuple[Any, AlertQueue, list[AgentState]]
) -> None:
    app, queue, _ = mock_app
    k8s_payload = {
        "reason": "OOMKilled",
        "type": "Warning",
        "namespace": "shop",
        "resource_kind": "pod",
        "resource_name": "backend-abc",
        "message": "Out of memory",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/events", json=k8s_payload)
        assert res.status_code == 202
        data = res.json()
        assert data["status"] == "accepted"
        assert data["enqueued"] is True
        assert data["alert"] == "OOMKilled"
        assert queue.queue.qsize() == 1


@pytest.mark.asyncio
async def test_end_to_end_webhook_to_graph_execution() -> None:
    executed_states: list[AgentState] = []

    def mock_graph_runner(state: AgentState) -> dict[str, Any]:
        executed_states.append(state)
        return {"outcome": "RESOLVED", "is_tier0": True}

    queue = AlertQueue(graph_executor=mock_graph_runner)
    app = create_app(alert_queue=queue)

    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "CoreDNSDown",
                    "severity": "critical",
                    "namespace": "kube-system",
                    "pod": "coredns-1",
                },
                "annotations": {"summary": "CoreDNS pods failing"},
            }
        ],
    }

    # Start app via lifespan context to launch worker loop
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Start queue worker
        await queue.start()
        try:
            res = await client.post("/alerts", json=payload)
            assert res.status_code == 202

            # Allow worker to dequeue and process
            import asyncio

            await asyncio.sleep(0.1)

            assert len(executed_states) == 1
            assert executed_states[0]["current_alert"].alertname == "CoreDNSDown"
            assert queue.total_processed == 1
        finally:
            await queue.stop()
