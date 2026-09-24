"""
Smoke test — verifies the project structure is importable
and the config loads correctly.

Run with: pytest agent/tests/unit/test_smoke.py -v
"""

import importlib


def test_package_importable() -> None:
    """The homeostat package must be importable."""
    import homeostat  # noqa: F401
    assert homeostat.__version__ == "0.1.0"


def test_config_loads() -> None:
    """Config must load with default values without errors."""
    from homeostat.config import settings

    assert settings.env in ("dev", "prod")
    assert settings.server_port == 8080
    assert settings.bedrock_model_id.startswith("anthropic.")
    assert settings.per_incident_budget_usd > 0
    assert settings.max_incident_retries > 0


def test_subpackages_importable() -> None:
    """All subpackages must be importable (even if empty stubs)."""
    subpackages = [
        "homeostat.nodes",
        "homeostat.tools",
        "homeostat.preprocessor",
        "homeostat.memory",
        "homeostat.tier0",
        "homeostat.llm",
    ]
    for pkg in subpackages:
        mod = importlib.import_module(pkg)
        assert mod is not None, f"Failed to import {pkg}"
