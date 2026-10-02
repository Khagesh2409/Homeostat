"""
AWS Bedrock LLM client.

Provides a simple interface to invoke Claude models via AWS Bedrock.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Haiku for speed and low cost
MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"

# Approximate costs per 1000 tokens for Haiku
COST_PER_1K_INPUT = 0.00025
COST_PER_1K_OUTPUT = 0.00125


def call_llm(prompt: str) -> tuple[str, dict[str, Any]]:
    """
    Call Claude via AWS Bedrock.
    
    Returns:
        A tuple of (response_text, usage_dict)
    """
    client = boto3.client("bedrock-runtime")

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.0,
    }

    try:
        import json
        response = client.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps(body),
        )

        response_body = json.loads(response.get("body").read())

        # Extract text response
        text = ""
        for content in response_body.get("content", []):
            if content.get("type") == "text":
                text += content.get("text", "")

        # Calculate usage and cost
        usage = response_body.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        cost_usd = (input_tokens / 1000.0 * COST_PER_1K_INPUT) + (output_tokens / 1000.0 * COST_PER_1K_OUTPUT)

        usage_info = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost_usd,
        }

        return text.strip(), usage_info

    except ClientError as e:
        logger.error("Bedrock API call failed: %s", e)
        raise
