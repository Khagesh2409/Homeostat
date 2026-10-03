"""
Homeostat LLM package — Bedrock client, cost tracking, and fallback handling.
"""

from homeostat.llm.client import (
    BedrockClient,
    BedrockUnavailableError,
    BudgetExceededError,
    LLMResponse,
    call_llm,
    call_llm_async,
    default_bedrock_client,
)
from homeostat.llm.cost import BudgetClient, CostTracker, get_token_pricing
from homeostat.llm.fallback import (
    CircuitBreaker,
    default_circuit_breaker,
    is_bedrock_available,
)

__all__ = [
    "BedrockClient",
    "BedrockUnavailableError",
    "BudgetClient",
    "BudgetExceededError",
    "CircuitBreaker",
    "CostTracker",
    "LLMResponse",
    "call_llm",
    "call_llm_async",
    "default_bedrock_client",
    "default_circuit_breaker",
    "get_token_pricing",
    "is_bedrock_available",
]
