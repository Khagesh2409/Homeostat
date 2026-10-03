"""
AWS Bedrock LLM client.

Provides backward-compatible interface to invoke Claude models via AWS Bedrock.
"""

from __future__ import annotations

from homeostat.llm.client import (
    BedrockClient,
    LLMResponse,
    call_llm,
    default_bedrock_client,
)
from homeostat.llm.cost import MODEL_PRICING

MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
COST_PER_1K_INPUT = MODEL_PRICING["haiku"]["input_per_m"] / 1000.0
COST_PER_1K_OUTPUT = MODEL_PRICING["haiku"]["output_per_m"] / 1000.0

__all__ = [
    "COST_PER_1K_INPUT",
    "COST_PER_1K_OUTPUT",
    "MODEL_ID",
    "BedrockClient",
    "LLMResponse",
    "call_llm",
    "default_bedrock_client",
]
