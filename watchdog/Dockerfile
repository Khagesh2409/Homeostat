# ──────────────────────────────────────────────────────────────
# Homeostat Watchdog — Dockerfile
# External safety supervisor container
# ──────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.cargo/bin:/usr/local/bin:$PATH"

# Install curl and ca-certificates for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv from official binary image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Copy dependency specifications for cached builds
COPY pyproject.toml uv.lock* ./

# Install dependencies using uv
RUN uv pip install --system -e .

# Copy application source code
COPY src/ ./src/

# Expose watchdog supervisor port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=20s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Launch Watchdog FastAPI service
CMD ["uvicorn", "watchdog.service:app", "--host", "0.0.0.0", "--port", "8000"]
