"""Unit tests for the chaos engineering framework, recorder, and runner."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from framework.recorder import ResultRecorder
from framework.runner import ScenarioRunner
from framework.scenario import ChaosResult, ChaosScenario


def test_scenario_and_result_serialization() -> None:
    """Test scenario instantiation and result dictionary serialization/deserialization."""
    scenario = ChaosScenario(
        name="test_pod_kill",
        category="pod",
        description="Kills a worker pod",
        attack=lambda: None,
        expected_detection_max_s=30.0,
        expected_recovery_max_s=60.0,
    )
    assert scenario.name == "test_pod_kill"
    assert scenario.category == "pod"

    result = ChaosResult(
        scenario="test_pod_kill",
        run_id="run-1234",
        detection_time_s=5.2,
        recovery_time_s=12.8,
        total_time_s=18.0,
        cost_usd=0.012,
        tier_used="tier0",
        success=True,
    )
    data = result.to_dict()
    assert data["run_id"] == "run-1234"
    assert data["detection_time_s"] == 5.2

    reconstructed = ChaosResult.from_dict(data)
    assert reconstructed.run_id == result.run_id
    assert reconstructed.total_time_s == result.total_time_s
    assert reconstructed.success is True


def test_result_recorder_and_learning_curve(tmp_path: Path) -> None:
    """Test recording runs to JSONL and calculating MTTR learning curves."""
    results_file = tmp_path / "runs.jsonl"
    recorder = ResultRecorder(local_path=results_file)

    # Run 1: Slow manual / LLM recovery (30s)
    r1 = ChaosResult(
        scenario="disk_fill",
        run_id="r1",
        timestamp=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        recovery_time_s=30.0,
        cost_usd=0.05,
        tier_used="llm_fresh",
        success=True,
    )
    # Run 2: Learned runbook recovery (12s)
    r2 = ChaosResult(
        scenario="disk_fill",
        run_id="r2",
        timestamp=datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
        recovery_time_s=12.0,
        cost_usd=0.01,
        tier_used="runbook",
        success=True,
    )
    # Run 3: Fast Tier-0 recovery (5s)
    r3 = ChaosResult(
        scenario="disk_fill",
        run_id="r3",
        timestamp=datetime(2026, 10, 3, 10, 0, tzinfo=UTC),
        recovery_time_s=5.0,
        cost_usd=0.0,
        tier_used="tier0",
        success=True,
    )

    recorder.record(r1)
    recorder.record(r2)
    recorder.record(r3)

    loaded = recorder.load_results("disk_fill")
    assert len(loaded) == 3

    curve = recorder.calculate_learning_curve()
    assert "disk_fill" in curve
    disk_metrics = curve["disk_fill"]
    assert disk_metrics["total_runs"] == 3
    assert disk_metrics["initial_recovery_s"] == 30.0
    assert disk_metrics["latest_recovery_s"] == 5.0
    assert disk_metrics["mttr_reduction_pct"] > 80.0
    assert disk_metrics["tier_distribution"]["tier0"] == 1
    assert disk_metrics["tier_distribution"]["llm_fresh"] == 1


def test_scenario_runner_execution_and_invariants(tmp_path: Path) -> None:
    """Test ScenarioRunner executing setup, attack, verifier, and recording."""
    results_file = tmp_path / "test_runner.jsonl"
    recorder = ResultRecorder(local_path=results_file)

    # Mock watchdog health probe and agent status
    mock_http = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "healthy", "mode": "idle", "active_incidents": 0}
    mock_http.get.return_value = resp

    runner = ScenarioRunner(
        recorder=recorder,
        http_client=mock_http,
    )

    attack_called = False
    setup_called = False
    cleanup_called = False

    def setup_fn() -> None:
        nonlocal setup_called
        setup_called = True

    def attack_fn() -> None:
        nonlocal attack_called
        attack_called = True

    def cleanup_fn() -> None:
        nonlocal cleanup_called
        cleanup_called = True

    scenario = ChaosScenario(
        name="sample_chaos",
        category="pod",
        description="Simulated attack",
        setup=setup_fn,
        attack=attack_fn,
        verify_recovered=lambda: True,  # Immediately recovers in test
        cleanup=cleanup_fn,
        safety_invariants=["Watchdog must be reachable"],
    )

    result = runner.run_scenario(scenario, poll_interval_s=0.01, max_timeout_s=2.0)

    assert result.success is True
    assert result.scenario == "sample_chaos"
    assert setup_called is True
    assert attack_called is True
    assert cleanup_called is True
    assert len(result.safety_violations) == 0

    # Ensure result recorded
    saved = recorder.load_results("sample_chaos")
    assert len(saved) == 1
    assert saved[0].success is True
