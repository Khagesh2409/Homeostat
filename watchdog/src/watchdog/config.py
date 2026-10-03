"""Configuration settings for the Homeostat external safety watchdog."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class WatchdogSettings(BaseSettings):
    """Configuration settings loaded from environment or defaults."""

    model_config = SettingsConfigDict(
        env_prefix="HOMEOSTAT_WATCHDOG_",
        env_file=".env",
        extra="ignore",
    )

    # AWS configuration
    aws_region: str = "us-east-1"
    agent_iam_role_name: str = "homeostat-agent"
    sns_topic_arn: str = ""

    # Agent network & Kubernetes configuration
    agent_base_url: str = "http://localhost:8080"
    agent_deployment_name: str = "homeostat-operator"
    agent_namespace: str = "homeostat"
    k8s_api_url: str = "https://kubernetes.default.svc"
    k8s_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"

    # Safety thresholds
    spend_cap_usd_month: float = 20.0
    spend_warning_threshold_usd: float = 16.0
    max_incidents_per_hour: int = 10
    max_pod_deletions_per_incident: int = 5
    heartbeat_timeout_seconds: int = 300
    health_check_interval_seconds: int = 60
    cloudwatch_max_api_calls_per_hour: int = 500

    # Service server settings
    host: str = "0.0.0.0"
    port: int = 8000


def get_settings() -> WatchdogSettings:
    """Return a cached or fresh instance of WatchdogSettings."""
    return WatchdogSettings()
