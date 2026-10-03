"""
Diagnose node — the first LLM call in the incident response path.

Given the alert + error signatures, asks the LLM to reason about the root cause.
Output is a structured diagnosis string, not raw LLM text.

Cost controls:
  - Only called when Tier-0 missed and no valid runbook found
  - Uses the cheapest model (Claude 3 Haiku via Bedrock)
  - Input is capped: max 10 error signatures, each max 300 chars
  - Token count and cost are tracked in state
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from homeostat.state import AgentState

logger = logging.getLogger(__name__)

_DIAGNOSIS_PROMPT = """\
You are an expert Kubernetes SRE analyzing a cluster incident.

## Incident
Alert: {alertname}
Severity: {severity}
Resource: {source} (namespace: {namespace})
Message: {message}

## Error Signatures (from log pre-processor)
{signatures}

## Task
Provide a concise root cause diagnosis. Be specific — name the likely cause, \
not just a description of symptoms.

Respond in this exact JSON format:
{{
  "root_cause": "<one sentence, specific cause>",
  "confidence": <0.0 to 1.0>,
  "affected_components": ["<component1>", "<component2>"],
  "recommended_action_type": "<restart_pod|delete_pod|scale_deployment|check_config|manual_review>"
}}
"""


def diagnose(state: AgentState) -> AgentState:
    """
    Call the LLM to diagnose the root cause of the incident.

    Input state keys:  current_alert, error_signatures, retry_count
    Output state keys: diagnosis, cost_usd, token_count, llm_calls, incident_log
    """
    from homeostat.llm.bedrock import call_llm

    log = list(state.get("incident_log", []))
    alert = state.get("current_alert")
    error_signatures = state.get("error_signatures", [])
    retry_count = state.get("retry_count", 0)

    if alert is None:
        log.append(f"[{_now()}] DIAGNOSE: No alert in state — skipping")
        return {"diagnosis": "unknown", "incident_log": log}

    # Format error signatures for the prompt
    if error_signatures:
        sig_text = "\n".join(
            f"  {i + 1}. {s.to_prompt_text()}" for i, s in enumerate(error_signatures[:10])
        )
    else:
        sig_text = "  (no error signatures — alert only)"

    prompt = _DIAGNOSIS_PROMPT.format(
        alertname=alert.alertname,
        severity=alert.severity.value,
        source=alert.source,
        namespace=alert.namespace,
        message=alert.message[:500],  # Cap message length
        signatures=sig_text,
    )

    if retry_count > 0:
        prompt += (
            f"\n\nNote: This is retry #{retry_count}. "
            "Previous attempts failed — consider a different approach."
        )

    log.append(f"[{_now()}] DIAGNOSE: Calling LLM for root cause analysis (retry={retry_count})")

    try:
        response, usage = call_llm(prompt)
        diagnosis_data = json.loads(response)
        diagnosis = diagnosis_data.get("root_cause", response)

        log.append(
            f"[{_now()}] DIAGNOSE: Root cause: '{diagnosis}' "
            f"(confidence={diagnosis_data.get('confidence', '?')}, "
            f"tokens={usage['total_tokens']}, "
            f"cost=${usage['cost_usd']:.4f})"
        )
        logger.info("Diagnosis: %s", diagnosis)

        return {
            "diagnosis": diagnosis,
            "cost_usd": state.get("cost_usd", 0.0) + usage["cost_usd"],
            "token_count": state.get("token_count", 0) + usage["total_tokens"],
            "llm_calls": state.get("llm_calls", 0) + 1,
            "incident_log": log,
        }

    except (json.JSONDecodeError, KeyError) as exc:
        # LLM returned non-JSON — use raw response as diagnosis
        logger.warning("LLM returned non-JSON diagnosis: %s", exc)
        log.append(f"[{_now()}] DIAGNOSE: LLM response parsing failed — using raw text")
        return {
            "diagnosis": response if "response" in dir() else "diagnosis_failed",
            "incident_log": log,
        }
    except Exception as exc:
        logger.error("Diagnose LLM call failed: %s", exc)
        log.append(f"[{_now()}] DIAGNOSE: LLM call failed ({exc}) — escalating")
        return {"diagnosis": "llm_call_failed", "incident_log": log}


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")
