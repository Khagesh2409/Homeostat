"""
Alert intake pipeline — parsing, deduplication, queuing, and background dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from homeostat.config import settings
from homeostat.graph import build_graph
from homeostat.metrics import record_incident
from homeostat.state import AgentState, Alert, AlertSeverity

logger = logging.getLogger(__name__)


def parse_alertmanager_alert(item: dict[str, Any]) -> Alert:
    """Parse a single alert dictionary from an Alertmanager webhook payload."""
    labels: dict[str, str] = item.get("labels", {})
    annotations: dict[str, str] = item.get("annotations", {})

    alertname = labels.get("alertname", "UnknownAlert")
    sev_str = labels.get("severity", "warning").lower()
    if sev_str in ("critical", "page", "fatal"):
        severity = AlertSeverity.CRITICAL
    elif sev_str in ("warning", "warn"):
        severity = AlertSeverity.WARNING
    else:
        severity = AlertSeverity.INFO

    namespace = labels.get("namespace", "default")

    # Determine best source representation
    pod = labels.get("pod")
    node = labels.get("node") or labels.get("instance")
    service = labels.get("service") or labels.get("job")

    if pod:
        source = pod if "/" in pod else f"pod/{pod}"
    elif node:
        source = node if "/" in node else f"node/{node}"
    elif service:
        source = service if "/" in service else f"service/{service}"
    else:
        source = "unknown"

    message = (
        annotations.get("message")
        or annotations.get("summary")
        or annotations.get("description")
        or ""
    )

    return Alert(
        alertname=alertname,
        severity=severity,
        namespace=namespace,
        source=source,
        message=message,
        labels=labels,
        annotations=annotations,
    )


def parse_alertmanager_payload(payload: dict[str, Any]) -> list[Alert]:
    """Parse all firing alerts from an Alertmanager webhook payload."""
    alerts_raw = payload.get("alerts", [])
    parsed: list[Alert] = []
    for item in alerts_raw:
        # Skip resolved alerts; only process active/firing
        if item.get("status") == "resolved":
            continue
        parsed.append(parse_alertmanager_alert(item))
    return parsed


def parse_k8s_event(payload: dict[str, Any]) -> Alert:
    """Parse a Kubernetes event webhook payload into an Alert."""
    return Alert.from_k8s_event(payload)


class AlertDeduplicator:
    """
    Deduplicates incoming alerts within a sliding time window.

    Prevents alert storms or repeated Alertmanager notifications from triggering
    duplicate concurrent graph executions.
    """

    def __init__(self, window_seconds: float | None = None) -> None:
        self.window_seconds = (
            window_seconds if window_seconds is not None else float(settings.tier0_cooldown_seconds)
        )
        self._seen: dict[str, float] = {}

    def is_duplicate(self, alert: Alert, now: float | None = None) -> bool:
        """
        Check if the alert was already seen recently.

        If not seen, registers the current timestamp and returns False.
        """
        current_time = now if now is not None else time.monotonic()
        key = alert.fingerprint

        # Prune expired entries
        self._prune(current_time)

        last_seen = self._seen.get(key)
        if last_seen is not None and (current_time - last_seen) < self.window_seconds:
            logger.debug(
                "Duplicate alert dropped: %s (seen %.1fs ago)",
                key,
                current_time - last_seen,
            )
            return True

        self._seen[key] = current_time
        return False

    def _prune(self, now: float) -> None:
        cutoff = now - (self.window_seconds * 2)
        expired = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in expired:
            del self._seen[k]

    def clear(self) -> None:
        """Clear all deduplication state."""
        self._seen.clear()


class AlertQueue:
    """
    In-memory async queue and background worker executing the LangGraph agent.
    """

    def __init__(
        self,
        deduplicator: AlertDeduplicator | None = None,
        graph_executor: Callable[[AgentState], dict[str, Any]] | None = None,
    ) -> None:
        self.queue: asyncio.Queue[Alert] = asyncio.Queue()
        self.deduplicator = deduplicator or AlertDeduplicator()
        self._custom_executor = graph_executor
        self._compiled_graph: CompiledStateGraph[Any, Any, Any] | None = None

        self._worker_task: asyncio.Task[None] | None = None
        self._running: bool = False

        # Status and observability
        self.total_received: int = 0
        self.total_enqueued: int = 0
        self.total_duplicates: int = 0
        self.total_processed: int = 0
        self.is_processing: bool = False
        self.active_incident: dict[str, Any] | None = None
        self.last_processed_at: datetime | None = None

    def _get_graph(self) -> CompiledStateGraph[Any, Any, Any]:
        if self._compiled_graph is None:
            self._compiled_graph = build_graph()
        return self._compiled_graph

    def enqueue(self, alert: Alert) -> bool:
        """
        Deduplicate and enqueue an alert.

        Returns True if enqueued, False if dropped as duplicate.
        """
        self.total_received += 1
        if self.deduplicator.is_duplicate(alert):
            self.total_duplicates += 1
            return False

        self.queue.put_nowait(alert)
        self.total_enqueued += 1
        logger.info(
            "Alert enqueued: %s on %s (queue_size=%d)",
            alert.alertname,
            alert.source,
            self.queue.qsize(),
        )
        return True

    async def start(self) -> None:
        """Start the background worker task."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("AlertQueue worker started.")

    async def stop(self) -> None:
        """Stop the background worker task gracefully."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        logger.info("AlertQueue worker stopped.")

    async def process_one(self, alert: Alert) -> dict[str, Any]:
        """Process a single alert through the LangGraph agent state machine."""
        self.is_processing = True
        start_time = time.monotonic()
        now_utc = datetime.now(UTC)

        self.active_incident = {
            "alertname": alert.alertname,
            "source": alert.source,
            "severity": alert.severity.value,
            "started_at": now_utc.isoformat(),
        }

        time_str = now_utc.strftime("%H:%M:%S")
        log_entry = f"[{time_str}] INTAKE: Processing alert {alert.alertname} on {alert.source}"
        state: AgentState = {
            "current_alert": alert,
            "incident_log": [log_entry],
            "retry_count": 0,
            "max_retries": settings.max_incident_retries,
        }

        try:
            if self._custom_executor is not None:
                final_state = await asyncio.to_thread(self._custom_executor, state)
            else:
                graph = self._get_graph()
                final_state = await asyncio.to_thread(graph.invoke, state)

            elapsed = time.monotonic() - start_time
            self.total_processed += 1
            self.last_processed_at = datetime.now(UTC)

            tier = "tier0" if final_state.get("is_tier0") else "llm"
            record_incident(
                severity=alert.severity.value,
                tier=tier,
                duration_seconds=elapsed,
            )

            logger.info(
                "Incident completed: %s on %s in %.2fs (tier=%s, outcome=%s)",
                alert.alertname,
                alert.source,
                elapsed,
                tier,
                final_state.get("outcome", "unknown"),
            )
            return final_state

        except Exception as exc:
            logger.error("Unhandled error processing incident %s: %s", alert.alertname, exc)
            return {"error": str(exc)}

        finally:
            self.is_processing = False
            self.active_incident = None

    async def _worker_loop(self) -> None:
        """Main loop pulling alerts from queue and executing them sequentially."""
        while self._running:
            try:
                alert = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                await self.process_one(alert)
                self.queue.task_done()
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in AlertQueue worker loop: %s", exc)

    def get_status(self) -> dict[str, Any]:
        """Return runtime queue statistics."""
        return {
            "queue_size": self.queue.qsize(),
            "total_received": self.total_received,
            "total_enqueued": self.total_enqueued,
            "total_duplicates": self.total_duplicates,
            "total_processed": self.total_processed,
            "is_processing": self.is_processing,
            "active_incident": self.active_incident,
            "last_processed_at": (
                self.last_processed_at.isoformat() if self.last_processed_at else None
            ),
        }


# Global queue singleton
default_alert_queue = AlertQueue()
