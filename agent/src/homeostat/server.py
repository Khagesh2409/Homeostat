"""
FastAPI HTTP server for Homeostat.

Exposes alert receivers, health probes, Prometheus metrics, and status endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from homeostat.config import settings
from homeostat.intake import (
    AlertQueue,
    default_alert_queue,
    parse_alertmanager_payload,
    parse_k8s_event,
)
from homeostat.llm.fallback import is_bedrock_available
from homeostat.metrics import get_metrics_output

logger = logging.getLogger(__name__)


def create_app(alert_queue: AlertQueue | None = None) -> FastAPI:
    """Create and configure the Homeostat FastAPI application."""
    queue = alert_queue or default_alert_queue

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: start queue background worker
        logger.info("Starting Homeostat agent server on port %d...", settings.server_port)
        await queue.start()
        yield
        # Shutdown: stop worker cleanly
        logger.info("Stopping Homeostat agent server...")
        await queue.stop()

    app = FastAPI(
        title="Homeostat Operator",
        description="Autonomous AI infrastructure operator with safety constraints",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.queue = queue

    # ── 1. Alert Receivers ────────────────────────────────────

    @app.post("/alerts", status_code=status.HTTP_202_ACCEPTED)
    async def receive_alerts(
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Alertmanager webhook receiver.
        Parses firing alerts, deduplicates them, and enqueues for LangGraph execution.
        """
        active_queue: AlertQueue = request.app.state.queue
        alerts = parse_alertmanager_payload(payload)

        enqueued_count = 0
        duplicate_count = 0
        for alert in alerts:
            if active_queue.enqueue(alert):
                enqueued_count += 1
            else:
                duplicate_count += 1

        logger.info(
            "Received %d alerts from Alertmanager (enqueued=%d, duplicates=%d)",
            len(alerts),
            enqueued_count,
            duplicate_count,
        )
        return {
            "status": "accepted",
            "received": len(alerts),
            "enqueued": enqueued_count,
            "duplicates": duplicate_count,
        }

    @app.post("/events", status_code=status.HTTP_202_ACCEPTED)
    async def receive_k8s_event(
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Kubernetes event receiver webhook.
        Converts K8s warning/error events into an Alert and enqueues.
        """
        active_queue: AlertQueue = request.app.state.queue
        alert = parse_k8s_event(payload)
        enqueued = active_queue.enqueue(alert)

        return {
            "status": "accepted",
            "enqueued": enqueued,
            "alert": alert.alertname,
            "source": alert.source,
        }

    # ── 2. Health & Readiness Probes ──────────────────────────

    @app.get("/health", status_code=status.HTTP_200_OK)
    async def health_liveness() -> dict[str, Any]:
        """Liveness probe for Kubernetes and the external Watchdog."""
        return {
            "status": "ok",
            "service": "homeostat",
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @app.get("/ready", status_code=status.HTTP_200_OK)
    async def readiness_probe(request: Request) -> JSONResponse:
        """Readiness probe indicating whether the agent is accepting alerts."""
        active_queue: AlertQueue = request.app.state.queue
        is_ready = active_queue._running
        return JSONResponse(
            content={
                "status": "ready" if is_ready else "not_ready",
                "queue_accepting": is_ready,
            },
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # ── 3. Prometheus Metrics ─────────────────────────────────

    @app.get("/metrics")
    async def get_metrics() -> Response:
        """Prometheus metrics endpoint exposing agent telemetry."""
        return Response(
            content=get_metrics_output(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── 4. Observability & Status ─────────────────────────────

    @app.get("/status")
    async def get_agent_status(request: Request) -> dict[str, Any]:
        """Detailed runtime status of the Homeostat agent."""
        active_queue: AlertQueue = request.app.state.queue
        bedrock_ok = is_bedrock_available()
        mode = "autonomous" if bedrock_ok else "degraded (tier0-only)"

        return {
            "service": "homeostat",
            "version": "0.1.0",
            "mode": mode,
            "bedrock_available": bedrock_ok,
            "queue": active_queue.get_status(),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # ── 5. Watchdog Protocol ──────────────────────────────────

    @app.post("/watchdog/ack")
    async def watchdog_ack(
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Acknowledge ping/heartbeat from the external Watchdog service.
        """
        data = payload or {}
        command = data.get("command", "heartbeat")
        logger.info("Watchdog ping received (command=%s)", command)
        return {
            "status": "acknowledged",
            "agent_id": "homeostat-agent",
            "healthy": True,
            "bedrock_available": is_bedrock_available(),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    return app


# Default ASGI application
app = create_app()
