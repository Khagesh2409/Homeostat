"""
Unit tests for AWS Bedrock client, cost tracker, circuit breaker, and budget enforcement.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from homeostat.llm.client import (
    BedrockClient,
    BedrockUnavailableError,
    BudgetExceededError,
    call_llm,
    call_llm_async,
)
from homeostat.llm.cost import BudgetClient, CostTracker, get_token_pricing
from homeostat.llm.fallback import CircuitBreaker, default_circuit_breaker


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    default_circuit_breaker.reset()


# ── CostTracker Tests ─────────────────────────────────────────────────────────


def test_token_pricing_rates() -> None:
    haiku_in, haiku_out = get_token_pricing("anthropic.claude-3-haiku-20240307-v1:0")
    assert haiku_in == 0.25 / 1_000_000.0
    assert haiku_out == 1.25 / 1_000_000.0

    sonnet_in, sonnet_out = get_token_pricing("anthropic.claude-3-sonnet-20240229-v1:0")
    assert sonnet_in == 3.00 / 1_000_000.0
    assert sonnet_out == 15.00 / 1_000_000.0


def test_cost_tracker_calculation() -> None:
    tracker = CostTracker(incident_id="inc-123", model_id="anthropic.claude-3-haiku-20240307-v1:0")

    # 1,000,000 input tokens ($0.25) + 1,000,000 output tokens ($1.25) = $1.50
    tracker.add_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert tracker.total_tokens == 2_000_000
    assert abs(tracker.cost_usd - 1.50) < 1e-6

    # Budget checks
    assert tracker.check_budget(limit_usd=2.00) is True
    assert tracker.check_budget(limit_usd=1.00) is False
    assert tracker.check_budget(limit_usd=1.50) is False


# ── BudgetClient Tests ────────────────────────────────────────────────────────


def test_budget_client_utilization() -> None:
    client = BudgetClient(account_id="123456789012")
    mock_boto = MagicMock()
    mock_boto.describe_budget.return_value = {
        "Budget": {
            "BudgetLimit": {"Amount": "100.0"},
            "CalculatedSpend": {"ActualSpend": {"Amount": "85.0"}},
        }
    }
    client._client = mock_boto

    utilization = client.get_monthly_budget_utilization("test-budget")
    assert utilization == 0.85

    # Should refuse non-critical warning at 85% with 80% threshold
    assert client.should_refuse_non_critical("warning", "test-budget", warn_pct=0.80) is True
    # Never refuse critical alerts
    assert client.should_refuse_non_critical("critical", "test-budget", warn_pct=0.80) is False


def test_budget_client_error_handling() -> None:
    client = BudgetClient(account_id="123456789012")
    mock_boto = MagicMock()
    mock_boto.describe_budget.side_effect = ClientError(
        {"Error": {"Code": "NotFoundException", "Message": "Not found"}}, "describe_budget"
    )
    client._client = mock_boto

    assert client.get_monthly_budget_utilization("test-budget") is None
    # If API fails, do not block non-critical
    assert client.should_refuse_non_critical("warning", "test-budget") is False


# ── CircuitBreaker Tests ──────────────────────────────────────────────────────


def test_circuit_breaker_trips_and_recovers() -> None:
    curr_time = [1000.0]

    breaker = CircuitBreaker(
        failure_threshold=3,
        cooldown_seconds=60.0,
        clock=lambda: curr_time[0],
    )

    assert breaker.is_available is True

    # Record 2 failures -> still available
    breaker.record_failure("error 1")
    breaker.record_failure("error 2")
    assert breaker.is_available is True

    # 3rd failure -> trips circuit
    breaker.record_failure("error 3")
    assert breaker.is_available is False

    # During cooldown -> remains unavailable
    curr_time[0] += 30.0
    assert breaker.is_available is False

    # Cooldown expires -> half-open
    curr_time[0] += 31.0
    assert breaker.is_available is True

    # Success resets breaker
    breaker.record_success()
    assert breaker.is_available is True


# ── BedrockClient Tests ───────────────────────────────────────────────────────


@patch("homeostat.llm.client.boto3.client")
def test_bedrock_client_generate_success(mock_boto: MagicMock) -> None:
    mock_runtime = MagicMock()
    mock_response = {
        "body": MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": "Pod restart required"}],
                "usage": {"input_tokens": 200, "output_tokens": 100},
            }).encode("utf-8")
        )
    }
    mock_runtime.invoke_model.return_value = mock_response
    mock_boto.return_value = mock_runtime

    client = BedrockClient()
    resp = client.generate("Analyze this error")

    assert resp.text == "Pod restart required"
    assert resp.input_tokens == 200
    assert resp.output_tokens == 100
    assert resp.total_tokens == 300
    assert resp.cost_usd > 0


@patch("homeostat.llm.client.boto3.client")
def test_bedrock_client_budget_exceeded(mock_boto: MagicMock) -> None:
    client = BedrockClient()
    with pytest.raises(BudgetExceededError, match="budget exceeded"):
        client.generate(
            "Analyze",
            current_spend_usd=0.55,
            budget_limit_usd=0.50,
        )


@patch("homeostat.llm.client.boto3.client")
def test_bedrock_client_circuit_breaker_open(mock_boto: MagicMock) -> None:
    default_circuit_breaker.record_failure("err1")
    default_circuit_breaker.record_failure("err2")
    default_circuit_breaker.record_failure("err3")

    client = BedrockClient()
    with pytest.raises(BedrockUnavailableError, match="circuit breaker is OPEN"):
        client.generate("Analyze")


@patch("homeostat.llm.client.boto3.client")
def test_bedrock_client_retry_on_throttling(mock_boto: MagicMock) -> None:
    mock_runtime = MagicMock()

    throttle_error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "InvokeModel",
    )
    success_response = {
        "body": MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": "Resolved after backoff"}],
                "usage": {"input_tokens": 50, "output_tokens": 20},
            }).encode("utf-8")
        )
    }

    # First call throttles, second call succeeds
    mock_runtime.invoke_model.side_effect = [throttle_error, success_response]
    mock_boto.return_value = mock_runtime

    client = BedrockClient()
    resp = client.generate("Prompt")
    assert resp.text == "Resolved after backoff"
    assert mock_runtime.invoke_model.call_count == 2


@pytest.mark.asyncio
@patch("homeostat.llm.client.boto3.client")
async def test_bedrock_client_async_call(mock_boto: MagicMock) -> None:
    mock_runtime = MagicMock()
    mock_response = {
        "body": MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": "Async result"}],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }).encode("utf-8")
        )
    }
    mock_runtime.invoke_model.return_value = mock_response
    mock_boto.return_value = mock_runtime

    client = BedrockClient()
    text, usage = await call_llm_async("Async test", client=client)
    assert text == "Async result"
    assert usage["total_tokens"] == 20


@patch("homeostat.llm.client.boto3.client")
def test_call_llm_convenience_function(mock_boto: MagicMock) -> None:
    mock_runtime = MagicMock()
    mock_response = {
        "body": MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": "Diagnosis OK"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }).encode("utf-8")
        )
    }
    mock_runtime.invoke_model.return_value = mock_response
    mock_boto.return_value = mock_runtime

    client = BedrockClient()
    text, usage = call_llm("Analyze", client=client)
    assert text == "Diagnosis OK"
    assert usage["total_tokens"] == 150
