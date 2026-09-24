# ──────────────────────────────────────────────────────────────
# Homeostat Makefile
# Usage: make <target>
# ──────────────────────────────────────────────────────────────

.PHONY: help dev-setup lint lint-fix test test-unit test-integration \
        build push deploy chaos-run clean

# Default target: show help
help:
	@echo ""
	@echo "  Homeostat — available targets:"
	@echo ""
	@echo "  Setup:"
	@echo "    make dev-setup        Install all dev tools and dependencies"
	@echo ""
	@echo "  Code quality:"
	@echo "    make lint             Run ruff + mypy (read-only)"
	@echo "    make lint-fix         Run ruff with auto-fix"
	@echo ""
	@echo "  Testing:"
	@echo "    make test             Run all tests (unit + integration)"
	@echo "    make test-unit        Run unit tests only (fast, no AWS)"
	@echo "    make test-integration Run integration tests (needs local stack)"
	@echo ""
	@echo "  Docker:"
	@echo "    make build            Build agent + watchdog Docker images"
	@echo "    make push             Push images to ECR"
	@echo ""
	@echo "  Deploy:"
	@echo "    make deploy           Apply K8s manifests to the cluster"
	@echo ""
	@echo "  Chaos:"
	@echo "    make chaos-run        Run all chaos scenarios"
	@echo ""
	@echo "  Local dev:"
	@echo "    make up               Start local dev stack (docker compose)"
	@echo "    make down             Stop local dev stack"
	@echo ""
	@echo "  Cleanup:"
	@echo "    make clean            Remove build artifacts"
	@echo ""

# ── Setup ──────────────────────────────────────────────────────

dev-setup:
	@echo "→ Installing uv..."
	@pip install uv --quiet
	@echo "→ Installing agent dev dependencies..."
	@cd agent && uv pip install -e ".[dev]" --quiet
	@echo "→ Installing watchdog dev dependencies..."
	@cd watchdog && uv pip install -e ".[dev]" --quiet
	@echo "→ Installing pre-commit hooks..."
	@uv pip install pre-commit --quiet
	@pre-commit install
	@echo ""
	@echo "✅ Dev setup complete. Run 'make test' to verify."

# ── Lint ───────────────────────────────────────────────────────

lint:
	@echo "→ Ruff (agent)..."
	@cd agent && uv run ruff check src/ tests/
	@echo "→ Ruff (watchdog)..."
	@cd watchdog && uv run ruff check src/ tests/
	@echo "→ Mypy (agent)..."
	@cd agent && uv run mypy src/homeostat/
	@echo "✅ Lint passed."

lint-fix:
	@echo "→ Ruff fix (agent)..."
	@cd agent && uv run ruff check --fix src/ tests/
	@echo "→ Ruff fix (watchdog)..."
	@cd watchdog && uv run ruff check --fix src/ tests/
	@echo "✅ Lint fix done."

# ── Test ───────────────────────────────────────────────────────

test:
	@echo "→ Running all tests..."
	@cd agent && uv run pytest tests/ -v
	@cd watchdog && uv run pytest tests/ -v
	@echo "✅ All tests passed."

test-unit:
	@echo "→ Running unit tests (no AWS, no network)..."
	@cd agent && uv run pytest tests/unit/ -v -m "not integration"
	@cd watchdog && uv run pytest tests/ -v

test-integration:
	@echo "→ Starting LocalStack..."
	@docker compose up localstack -d
	@echo "→ Waiting for LocalStack to be ready..."
	@sleep 5
	@echo "→ Running integration tests..."
	@cd agent && uv run pytest tests/integration/ -v -m "integration"
	@echo "✅ Integration tests done."

# ── Docker ─────────────────────────────────────────────────────

build:
	@echo "→ Building agent image..."
	@docker build -t homeostat-agent:latest ./agent/
	@echo "→ Building watchdog image..."
	@docker build -t homeostat-watchdog:latest ./watchdog/
	@echo "✅ Images built."

push:
	@echo "→ Getting ECR login..."
	@aws ecr get-login-password --region us-east-1 | \
		docker login --username AWS --password-stdin \
		$(shell aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com
	@echo "→ Tagging and pushing agent..."
	@docker tag homeostat-agent:latest \
		$(shell aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/homeostat-agent:latest
	@docker push \
		$(shell aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/homeostat-agent:latest
	@echo "→ Tagging and pushing watchdog..."
	@docker tag homeostat-watchdog:latest \
		$(shell aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/homeostat-watchdog:latest
	@docker push \
		$(shell aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/homeostat-watchdog:latest
	@echo "✅ Images pushed to ECR."

# ── Deploy ─────────────────────────────────────────────────────

deploy:
	@echo "→ Applying K8s manifests (prod overlay)..."
	@kubectl apply -k k8s/overlays/prod/
	@echo "→ Waiting for agent rollout..."
	@kubectl rollout status deployment/homeostat-agent -n homeostat --timeout=120s
	@echo "✅ Deploy complete."

deploy-dev:
	@echo "→ Applying K8s manifests (dev overlay)..."
	@kubectl apply -k k8s/overlays/dev/
	@echo "✅ Dev deploy complete."

# ── Local Dev Stack ────────────────────────────────────────────

up:
	@echo "→ Starting local dev stack..."
	@docker compose up -d
	@echo "✅ Stack running. Services:"
	@echo "   Prometheus:    http://localhost:9090"
	@echo "   Alertmanager:  http://localhost:9093"
	@echo "   LocalStack:    http://localhost:4566"
	@echo "   Agent:         http://localhost:8080"

down:
	@docker compose down
	@echo "✅ Stack stopped."

# ── Chaos Testing ──────────────────────────────────────────────

chaos-run:
	@echo "→ Running chaos test suite..."
	@cd chaos && uv run python -m framework.runner --all
	@echo "✅ Chaos run complete. Results in chaos/results/"

chaos-scenario:
	@echo "→ Running scenario: $(SCENARIO)"
	@cd chaos && uv run python -m framework.runner --scenario $(SCENARIO)

# ── Cleanup ────────────────────────────────────────────────────

clean:
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@find . -name ".coverage" -delete 2>/dev/null || true
	@echo "✅ Cleaned up."
