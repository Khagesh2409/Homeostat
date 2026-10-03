"""
AWS Bedrock LLM client with retries, cost tracking, and circuit breaker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from homeostat.config import settings
from homeostat.llm.cost import get_token_pricing
from homeostat.llm.fallback import default_circuit_breaker

logger = logging.getLogger(__name__)

# Throttling and temporary service error codes for tenacity retry
RETRYABLE_BEDROCK_ERRORS = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
})


class BedrockUnavailableError(Exception):
    """Raised when Bedrock circuit breaker is open or service is unreachable."""


class BudgetExceededError(Exception):
    """Raised when per-incident budget limit is exceeded."""


def _is_retryable_error(exc: BaseException) -> bool:
    """Check if exception is a retryable Bedrock client error."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in RETRYABLE_BEDROCK_ERRORS
    return False


@dataclass(frozen=True)
class LLMResponse:
    """Structured response from Bedrock invocation."""

    text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float


class BedrockClient:
    """AWS Bedrock client managing model invocation, retries, and costs."""

    _client: Any

    def __init__(
        self,
        model_id: str | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self.model_id = model_id or settings.bedrock_model_id
        self.region_name = region_name or settings.aws_region
        self.endpoint_url = endpoint_url or settings.aws_endpoint_url

        kwargs: dict[str, Any] = {"region_name": self.region_name}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        self._client = boto3.client("bedrock-runtime", **kwargs)

    @retry(
        retry=retry_if_exception(_is_retryable_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _invoke_with_retry(self, body_json: str) -> dict[str, Any]:
        """Invoke Bedrock runtime with exponential backoff on throttling."""
        response = self._client.invoke_model(
            modelId=self.model_id,
            body=body_json,
        )
        body = response.get("body")
        if body is None:
            return {}
        return json.loads(body.read())  # type: ignore[no-any-return]

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        current_spend_usd: float | None = None,
        budget_limit_usd: float | None = None,
    ) -> LLMResponse:
        """
        Invoke Claude via Bedrock with circuit breaker and budget checks.
        """
        # 1. Budget check
        limit = (
            budget_limit_usd
            if budget_limit_usd is not None
            else settings.per_incident_budget_usd
        )
        if current_spend_usd is not None and current_spend_usd >= limit:
            raise BudgetExceededError(
                f"Incident budget exceeded: spent ${current_spend_usd:.4f} >= limit ${limit:.4f}"
            )

        # 2. Circuit breaker check
        if not default_circuit_breaker.is_available:
            raise BedrockUnavailableError(
                "Bedrock circuit breaker is OPEN. Service degraded, falling back to Tier-0."
            )

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens or settings.bedrock_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature if temperature is not None else settings.bedrock_temperature,
        }

        try:
            response_data = self._invoke_with_retry(json.dumps(body))
            default_circuit_breaker.record_success()

            # Extract response text
            text = ""
            for item in response_data.get("content", []):
                if item.get("type") == "text":
                    text += item.get("text", "")

            # Token usage and pricing
            usage = response_data.get("usage", {})
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            total_tokens = input_tokens + output_tokens

            input_rate, output_rate = get_token_pricing(self.model_id)
            cost = (input_tokens * input_rate) + (output_tokens * output_rate)

            return LLMResponse(
                text=text.strip(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
            )

        except Exception as exc:
            default_circuit_breaker.record_failure(exc)
            logger.error("Bedrock generation failed: %s", exc)
            raise

    async def generate_async(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        current_spend_usd: float | None = None,
        budget_limit_usd: float | None = None,
    ) -> LLMResponse:
        """Asynchronous invocation of generate using thread executor."""
        return await asyncio.to_thread(
            self.generate,
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            current_spend_usd=current_spend_usd,
            budget_limit_usd=budget_limit_usd,
        )


default_bedrock_client = BedrockClient()


def call_llm(
    prompt: str,
    *,
    client: BedrockClient | None = None,
    current_spend_usd: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Unified entry point for calling Bedrock.

    Returns:
        tuple of (response_text, usage_info_dict)
    """
    bedrock = client or BedrockClient()
    resp = bedrock.generate(prompt, current_spend_usd=current_spend_usd)

    usage_info = {
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "total_tokens": resp.total_tokens,
        "cost_usd": resp.cost_usd,
    }
    return resp.text, usage_info


async def call_llm_async(
    prompt: str,
    *,
    client: BedrockClient | None = None,
    current_spend_usd: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Asynchronous entry point for calling Bedrock."""
    bedrock = client or BedrockClient()
    resp = await bedrock.generate_async(prompt, current_spend_usd=current_spend_usd)

    usage_info = {
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "total_tokens": resp.total_tokens,
        "cost_usd": resp.cost_usd,
    }
    return resp.text, usage_info
