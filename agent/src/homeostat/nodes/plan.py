"""
Plan node — the second LLM call. Generates an actionable recovery plan.

Takes the diagnosis and outputs a structured ActionPlan (a list of ActionSteps).
Tools available to the LLM are described in the prompt.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from homeostat.state import ActionPlan, ActionStep, AgentState

logger = logging.getLogger(__name__)

_PLAN_PROMPT = """\
You are an expert Kubernetes SRE. Your task is to generate a recovery plan for an incident.

## Incident Context
Alert: {alertname}
Resource: {source} (namespace: {namespace})
Diagnosis: {diagnosis}

## Available Tools
You can use the following tools in your plan:
1. "kubectl": Run kubectl commands.
   Args: {{"command": "<full kubectl command, e.g., 'delete pod nginx -n default'>"}}
2. "helm": Run helm commands.
   Args: {{"command": "<full helm command>"}}
3. "system": Run node-level diagnostic commands (read-only).
   Args: {{"command": "<df, top, netstat, etc.>"}}

## Constraints
- The plan MUST be a sequence of steps.
- Every step MUST use one of the tools above.
- Be conservative. Do not delete stateful data (PVCs, databases) unless absolutely necessary.
- If the diagnosis is "unknown", the plan should focus on data gathering (e.g., describing the pod or checking node resources).

Respond in this exact JSON format:
{{
  "rationale": "<brief explanation of why this plan will work>",
  "estimated_risk": "<low|medium|high>",
  "steps": [
    {{
      "tool": "<tool_name>",
      "command": "<command_string>",
      "description": "<what this step does>"
    }}
  ]
}}
"""


def plan(state: AgentState) -> AgentState:
    """
    Call the LLM to generate an action plan based on the diagnosis.

    Input state keys:  current_alert, diagnosis, retry_count
    Output state keys: plan, cost_usd, token_count, llm_calls, incident_log
    """
    from homeostat.llm.bedrock import call_llm

    log = list(state.get("incident_log", []))
    alert = state.get("current_alert")
    diagnosis = state.get("diagnosis", "unknown")

    if not alert:
        log.append(f"[{_now()}] PLAN: No alert in state — skipping")
        return {"incident_log": log}

    prompt = _PLAN_PROMPT.format(
        alertname=alert.alertname,
        source=alert.source,
        namespace=alert.namespace,
        diagnosis=diagnosis,
    )

    log.append(f"[{_now()}] PLAN: Generating recovery plan via LLM")

    try:
        response, usage = call_llm(prompt)
        plan_data = json.loads(response)

        steps = []
        for s in plan_data.get("steps", []):
            steps.append(
                ActionStep(
                    tool=s.get("tool", "kubectl"),
                    command=s.get("command", ""),
                    args={"command": s.get("command", "")},
                    description=s.get("description", ""),
                )
            )

        action_plan = ActionPlan(
            steps=steps,
            rationale=plan_data.get("rationale", ""),
            estimated_risk=plan_data.get("estimated_risk", "medium"),  # type: ignore
        )

        log.append(
            f"[{_now()}] PLAN: Generated {len(steps)} steps "
            f"(risk={action_plan.estimated_risk}, "
            f"tokens={usage['total_tokens']}, cost=${usage['cost_usd']:.4f})"
        )

        return {
            "plan": action_plan,
            "cost_usd": state.get("cost_usd", 0.0) + usage["cost_usd"],
            "token_count": state.get("token_count", 0) + usage["total_tokens"],
            "llm_calls": state.get("llm_calls", 0) + 1,
            "incident_log": log,
        }

    except Exception as exc:
        logger.error("Plan LLM call failed: %s", exc)
        log.append(f"[{_now()}] PLAN: Failed to generate plan ({exc}) — escalating")
        return {"incident_log": log}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")
