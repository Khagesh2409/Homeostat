"""
Cost tracking and budget management for LLM invocations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from homeostat.config import settings

logger = logging.getLogger(__name__)

# Rates per 1,000,000 tokens (USD)
# Claude 3 Haiku: $0.25 / M input, $1.25 / M output
# Claude 3 Sonnet / 3.5 Sonnet: $3.00 / M input, $15.00 / M output
MODEL_PRICING: dict[str, dict[str, float]] = {
    "haiku": {
        "input_per_m": 0.25,
        "output_per_m": 1.25,
    },
    "sonnet": {
        "input_per_m": 3.00,
        "output_per_m": 15.00,
    },
}


def get_token_pricing(model_id: str) -> tuple[float, float]:
    """Return (cost_per_token_input, cost_per_token_output) in USD."""
    lower_id = model_id.lower()
    if "sonnet" in lower_id:
        rates = MODEL_PRICING["sonnet"]
    else:
        rates = MODEL_PRICING["haiku"]

    input_cost = rates["input_per_m"] / 1_000_000.0
    output_cost = rates["output_per_m"] / 1_000_000.0
    return input_cost, output_cost


@dataclass
class CostTracker:
    """Tracks token consumption and spend per incident."""

    incident_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    model_id: str = settings.bedrock_model_id

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        input_rate, output_rate = get_token_pricing(self.model_id)
        return (self.input_tokens * input_rate) + (self.output_tokens * output_rate)

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def check_budget(self, limit_usd: float | None = None) -> bool:
        """True if current spend is strictly under the allowed budget."""
        limit = limit_usd if limit_usd is not None else settings.per_incident_budget_usd
        return self.cost_usd < limit


class BudgetClient:
    """Queries AWS Budgets API to monitor monthly spend."""

    _client: Any

    def __init__(self, account_id: str | None = None) -> None:
        self._account_id = account_id
        self._client = None
        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.aws_endpoint_url:
            kwargs["endpoint_url"] = settings.aws_endpoint_url

        try:
            self._client = boto3.client("budgets", **kwargs)
        except Exception as exc:
            logger.warning("Could not initialize AWS Budgets client: %s", exc)
            self._client = None

    def get_monthly_budget_utilization(
        self, budget_name: str = "homeostat-monthly-budget"
    ) -> float | None:
        """
        Return the percentage of monthly budget spent (e.g. 0.85 for 85%).
        Returns None if Budgets API is unreachable or not configured.
        """
        if self._client is None or not self._account_id:
            return None

        try:
            resp = self._client.describe_budget(
                AccountId=self._account_id,
                BudgetName=budget_name,
            )
            budget = resp.get("Budget", {})
            spend = budget.get("CalculatedSpend", {}).get("ActualSpend", {})
            actual = float(spend.get("Amount", 0.0))
            limit = float(budget.get("BudgetLimit", {}).get("Amount", 0.0))
            if limit <= 0:
                return 0.0
            return actual / limit
        except ClientError as exc:
            logger.info("AWS Budget query failed (%s)", exc)
            return None

    def should_refuse_non_critical(
        self,
        severity: str,
        budget_name: str = "homeostat-monthly-budget",
        warn_pct: float | None = None,
    ) -> bool:
        """
        Refuse non-critical work if monthly budget consumption exceeds warning threshold.
        Critical incidents are never refused.
        """
        if severity.lower() == "critical":
            return False

        threshold = warn_pct if warn_pct is not None else settings.monthly_budget_warn_pct
        utilization = self.get_monthly_budget_utilization(budget_name)
        if utilization is not None and utilization >= threshold:
            logger.warning(
                "Refusing non-critical alert (severity=%s): monthly budget utilization "
                "%.1f%% >= threshold %.1f%%",
                severity,
                utilization * 100.0,
                threshold * 100.0,
            )
            return True
        return False
