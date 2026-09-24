"""
Homeostat configuration — all tunables in one place.
Values are read from environment variables with sensible defaults.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All agent configuration, loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="HOMEOSTAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Environment ──────────────────────────────────────────
    env: str = "dev"  # "dev" | "prod"
    log_level: str = "INFO"

    # ── AWS ──────────────────────────────────────────────────
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None  # Set to LocalStack URL in dev

    # ── Bedrock ──────────────────────────────────────────────
    bedrock_model_id: str = "anthropic.claude-3-haiku-20240307-v1:0"
    bedrock_max_tokens: int = 2048
    bedrock_temperature: float = 0.0  # Deterministic — no creativity needed

    # ── Memory (DynamoDB) ────────────────────────────────────
    memory_table: str = "homeostat-runbooks"
    state_table: str = "homeostat-state"
    s3_bucket: str = "homeostat-memory"

    # ── Budget controls ──────────────────────────────────────
    per_incident_budget_usd: float = 0.50   # Max spend per incident
    monthly_budget_warn_pct: float = 0.80   # Refuse non-critical at 80% of monthly budget

    # ── Agent behaviour ──────────────────────────────────────
    max_incident_retries: int = 3           # Give up after 3 failed attempts
    tier0_cooldown_seconds: int = 60        # Don't repeat Tier-0 action too fast
    log_window_seconds: int = 60            # Tumbling window for log clustering
    max_signatures_per_window: int = 10     # Cap how many error clusters we show the LLM

    # ── Server ───────────────────────────────────────────────
    server_host: str = "0.0.0.0"
    server_port: int = 8080

    # ── External services ────────────────────────────────────
    alertmanager_url: str = "http://localhost:9093"
    watchdog_url: str = "http://localhost:8081"
    watchdog_heartbeat_interval_seconds: int = 60


# Singleton — import this everywhere
settings = Settings()
