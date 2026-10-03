"""Watchdog FastAPI service and background safety monitoring loop."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
import structlog
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from watchdog.config import WatchdogSettings, get_settings
from watchdog.killswitch import KillSwitch
from watchdog.rules import RuleEvaluation, RuleSeverity, WatchdogRuleEngine

logger = structlog.get_logger(__name__)


# ── Request / Response Models ─────────────────────────────────────────────────


class HeartbeatRequest(BaseModel):
    """Payload sent by the agent during heartbeat."""

    agent_id: str = Field(default="homeostat-agent")
    timestamp: str | None = None
    active_incidents: int = Field(default=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentActionRequest(BaseModel):
    """Payload sent by the agent when performing an incident action."""

    incident_id: str
    action: str = ""
    target: str = ""
    pod_deletions: int = 0
    namespace: str = ""


class VerifyActionRequest(BaseModel):
    """Payload for pre-verifying an action before execution."""

    incident_id: str
    action: str
    target: str = ""
    pod_deletions: int = 0
    namespace: str = ""


class KillRequest(BaseModel):
    """Payload for manually invoking the kill switch."""

    reason: str = Field(default="Manual kill switch invocation")


class RestoreRequest(BaseModel):
    """Payload for restoring agent after a kill switch event."""

    reason: str = Field(default="Manual restoration request")


# ── Watchdog Service Controller ───────────────────────────────────────────────


class WatchdogService:
    """Core watchdog controller managing rule evaluation and background monitoring."""

    def __init__(
        self,
        settings: WatchdogSettings | None = None,
        rule_engine: WatchdogRuleEngine | None = None,
        killswitch: KillSwitch | None = None,
        ce_client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.rule_engine = rule_engine or WatchdogRuleEngine(
            spend_cap_usd=self.settings.spend_cap_usd_month,
            spend_warning_usd=self.settings.spend_warning_threshold_usd,
            max_incidents_per_hour=self.settings.max_incidents_per_hour,
            max_pod_deletions_per_incident=self.settings.max_pod_deletions_per_incident,
            heartbeat_timeout_seconds=self.settings.heartbeat_timeout_seconds,
        )
        self.killswitch = killswitch or KillSwitch(settings=self.settings)
        self._ce_client = ce_client or boto3.client(
            "ce",
            region_name=self.settings.aws_region,
        )
        self.current_spend_usd: float = 0.0
        self._monitoring_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None

    async def get_http_client(self) -> httpx.AsyncClient:
        """Return or create reusable HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    def fetch_aws_spend(self) -> float:
        """Query AWS Cost Explorer for month-to-date unblended spend."""
        today = datetime.now(UTC).date()
        start_of_month = today.replace(day=1).isoformat()
        # Cost Explorer End date is exclusive; if today is 1st, use tomorrow
        end_date = (
            today.isoformat()
            if today.day > 1
            else today.replace(day=2).isoformat()
        )

        try:
            response = self._ce_client.get_cost_and_usage(
                TimePeriod={"Start": start_of_month, "End": end_date},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            results = response.get("ResultsByTime", [])
            if results:
                cost_str = (
                    results[0]
                    .get("Total", {})
                    .get("UnblendedCost", {})
                    .get("Amount", "0.0")
                )
                self.current_spend_usd = float(cost_str)
                logger.info(
                    "aws_spend_queried",
                    month_to_date_usd=self.current_spend_usd,
                )
        except ClientError as e:
            logger.warning("aws_cost_explorer_query_failed", error=str(e))
        except (ValueError, KeyError, OSError) as e:
            logger.warning("aws_cost_explorer_error", error=str(e))

        return self.current_spend_usd

    async def check_agent_health(self) -> bool:
        """Ping agent health endpoint."""
        client = await self.get_http_client()
        url = f"{self.settings.agent_base_url}/health"
        try:
            resp = await client.get(url)
            return resp.status_code == status.HTTP_200_OK
        except (httpx.HTTPError, OSError) as e:
            logger.warning("agent_health_check_failed", url=url, error=str(e))
            return False

    async def run_safety_cycle(self) -> list[RuleEvaluation]:
        """Perform one complete evaluation cycle across all safety rules."""
        # 1. Fetch AWS spend
        spend = self.fetch_aws_spend()

        # 2. Check agent responsiveness
        await self.check_agent_health()

        # 3. Evaluate periodic rules
        evaluations = self.rule_engine.evaluate_periodic_health(
            current_spend_usd=spend,
            current_time=datetime.now(UTC),
        )

        # 4. Trigger kill switch if any critical violation occurs
        for ev in evaluations:
            if not ev.passed and ev.severity == RuleSeverity.VIOLATION:
                if not self.killswitch.is_activated:
                    logger.critical(
                        "safety_rule_violation_triggering_killswitch",
                        rule=ev.rule_name,
                        message=ev.message,
                    )
                    await self.killswitch.activate(
                        reason=ev.message,
                        trigger_source=ev.rule_name,
                    )
                break

        return evaluations

    async def start_monitoring_loop(self) -> None:
        """Background monitoring loop running every configured interval."""
        interval = self.settings.health_check_interval_seconds
        logger.info(
            "watchdog_monitoring_loop_started",
            interval_seconds=interval,
        )

        while True:
            try:
                await self.run_safety_cycle()
            except asyncio.CancelledError:
                logger.info("watchdog_monitoring_loop_cancelled")
                break
            except Exception as e:  # noqa: BLE001
                # Outer loop must catch all exceptions to prevent supervisor thread death
                logger.error("watchdog_monitoring_cycle_error", error=str(e))

            await asyncio.sleep(interval)

    async def stop(self) -> None:
        """Stop background tasks and close resources."""
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


# Global service instance
watchdog_service = WatchdogService()


# ── FastAPI Lifespan & Application ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start background health check loop on startup and terminate on shutdown."""
    watchdog_service._monitoring_task = asyncio.create_task(
        watchdog_service.start_monitoring_loop()
    )
    yield
    await watchdog_service.stop()


app = FastAPI(
    title="Homeostat External Watchdog",
    description="External safety supervisor enforcing hard constraints on the Homeostat agent",
    version="0.1.0",
    lifespan=lifespan,
)


# ── REST API Routes ───────────────────────────────────────────────────────────


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Watchdog self-health check."""
    return {
        "status": "healthy",
        "killswitch_active": watchdog_service.killswitch.is_activated,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@app.get("/status")
async def status_report() -> dict[str, Any]:
    """Detailed status report of watchdog rules, spend, and kill switch."""
    now = datetime.now(UTC)
    last_hb = watchdog_service.rule_engine.heartbeat_rule.last_heartbeat_at
    actions_count = watchdog_service.rule_engine.rate_limit_rule.get_action_count(now)

    return {
        "killswitch_active": watchdog_service.killswitch.is_activated,
        "killswitch_reason": watchdog_service.killswitch.activation_reason,
        "activated_at": (
            watchdog_service.killswitch.activated_at.isoformat()
            if watchdog_service.killswitch.activated_at
            else None
        ),
        "actions_last_hour": actions_count,
        "max_incidents_per_hour": watchdog_service.settings.max_incidents_per_hour,
        "current_spend_usd": watchdog_service.current_spend_usd,
        "spend_cap_usd": watchdog_service.settings.spend_cap_usd_month,
        "last_heartbeat_at": last_hb.isoformat() if last_hb else None,
        "total_killswitch_events": len(watchdog_service.killswitch.history),
    }


@app.post("/heartbeat")
async def receive_heartbeat(payload: HeartbeatRequest) -> dict[str, Any]:
    """Record agent heartbeat ping and check kill switch status."""
    watchdog_service.rule_engine.heartbeat_rule.record_heartbeat(
        timestamp=datetime.now(UTC),
        metadata=payload.metadata,
    )

    return {
        "status": "acknowledged",
        "killswitch_active": watchdog_service.killswitch.is_activated,
        "server_time": datetime.now(UTC).isoformat(),
    }


@app.post("/verify_action")
async def verify_action(payload: VerifyActionRequest) -> dict[str, Any]:
    """Pre-flight verify if an action conforms to scope and blast radius rules."""
    evaluations: list[RuleEvaluation] = []

    # 1. Scope boundary verification
    scope_eval = watchdog_service.rule_engine.scope_rule.evaluate_target(
        target_resource=payload.target,
        action=payload.action,
    )
    evaluations.append(scope_eval)

    # 2. Namespace protection
    if payload.namespace:
        ns_eval = watchdog_service.rule_engine.blast_radius_rule.evaluate_namespace_operation(
            action=payload.action,
            namespace=payload.namespace,
        )
        evaluations.append(ns_eval)

    # 3. Pod deletion count evaluation
    if payload.pod_deletions > 0:
        pod_eval = watchdog_service.rule_engine.blast_radius_rule.evaluate_pod_deletion(
            incident_id=payload.incident_id,
            count=payload.pod_deletions,
        )
        evaluations.append(pod_eval)

    has_violation = any(not e.passed for e in evaluations)
    return {
        "allowed": not has_violation,
        "evaluations": [
            {
                "rule_name": e.rule_name,
                "passed": e.passed,
                "severity": e.severity.value,
                "message": e.message,
            }
            for e in evaluations
        ],
    }


@app.post("/incident_action")
async def record_incident_action(payload: IncidentActionRequest) -> dict[str, Any]:
    """Record an incident action, enforcing rate limits and blast radius."""
    if watchdog_service.killswitch.is_activated:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kill switch is active. All agent actions are halted.",
        )

    # 1. Verify scope boundary
    scope_eval = watchdog_service.rule_engine.scope_rule.evaluate_target(
        target_resource=payload.target,
        action=payload.action,
    )
    if not scope_eval.passed:
        await watchdog_service.killswitch.activate(
            reason=scope_eval.message,
            trigger_source="ScopeBoundaryRule",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=scope_eval.message,
        )

    # 2. Verify blast radius on pods
    if payload.pod_deletions > 0:
        pod_eval = watchdog_service.rule_engine.blast_radius_rule.evaluate_pod_deletion(
            incident_id=payload.incident_id,
            count=payload.pod_deletions,
        )
        if not pod_eval.passed:
            await watchdog_service.killswitch.activate(
                reason=pod_eval.message,
                trigger_source="BlastRadiusRule",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=pod_eval.message,
            )
        watchdog_service.rule_engine.blast_radius_rule.record_pod_deletions(
            incident_id=payload.incident_id,
            count=payload.pod_deletions,
        )

    # 3. Record in rate limiter
    watchdog_service.rule_engine.rate_limit_rule.record_action(
        incident_id=payload.incident_id,
        timestamp=datetime.now(UTC),
    )

    # 4. Check if rate limit is exceeded
    rate_eval = watchdog_service.rule_engine.rate_limit_rule.evaluate()
    if not rate_eval.passed:
        logger.critical(
            "rate_limit_exceeded_triggering_killswitch",
            incident_id=payload.incident_id,
        )
        await watchdog_service.killswitch.activate(
            reason=rate_eval.message,
            trigger_source="RateLimitRule",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=rate_eval.message,
        )

    return {
        "status": "approved",
        "incident_id": payload.incident_id,
        "actions_in_last_hour": watchdog_service.rule_engine.rate_limit_rule.get_action_count(),
    }


@app.post("/kill")
async def manual_kill(payload: KillRequest) -> dict[str, Any]:
    """Manually activate emergency kill switch."""
    record = await watchdog_service.killswitch.activate(
        reason=payload.reason,
        trigger_source="manual_api_call",
    )
    return {
        "status": "activated",
        "action": record.action,
        "reason": record.reason,
        "timestamp": record.timestamp.isoformat(),
        "iam_revoked": record.iam_revoked,
        "k8s_scaled": record.k8s_scaled,
        "sns_alert_sent": record.sns_alert_sent,
    }


@app.post("/restore")
async def manual_restore(payload: RestoreRequest) -> dict[str, Any]:
    """Manually restore agent operations."""
    record = await watchdog_service.killswitch.restore(
        reason=payload.reason,
    )
    return {
        "status": "restored",
        "action": record.action,
        "reason": record.reason,
        "timestamp": record.timestamp.isoformat(),
        "iam_revoked": record.iam_revoked,
        "k8s_scaled": record.k8s_scaled,
        "sns_alert_sent": record.sns_alert_sent,
    }
