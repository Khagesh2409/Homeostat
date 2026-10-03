"""Unit tests for the learning curve measurement, statistical analysis, and plot generator."""

from pathlib import Path

from analysis.plot import plot_learning_curves
from analysis.stats import (
    calculate_learning_curve_stats,
    calculate_mean,
    calculate_median,
    calculate_p95,
    calculate_std,
    two_sample_t_test,
)
from experiments.learning_curve import LearningCurveExperiment
from framework.scenario import ChaosResult, ChaosScenario


def test_basic_statistical_functions() -> None:
    """Test arithmetic mean, sample std, median, and 95th percentile calculations."""
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert calculate_mean(data) == 30.0
    assert round(calculate_std(data), 2) == 15.81
    assert calculate_median(data) == 30.0
    assert calculate_p95(data) == 50.0

    # Even length median
    assert calculate_median([10.0, 20.0, 30.0, 40.0]) == 25.0

    # Empty lists
    assert calculate_mean([]) == 0.0
    assert calculate_std([]) == 0.0
    assert calculate_median([]) == 0.0
    assert calculate_p95([]) == 0.0


def test_two_sample_t_test_calculation() -> None:
    """Test Welch's t-test comparing two distinct distribution samples."""
    # Distinct distributions: sample A high recovery, sample B low recovery
    sample_a = [30.0, 32.0, 29.0, 31.0, 33.0]
    sample_b = [5.0, 6.0, 5.5, 4.8, 5.2]

    t_stat, p_val = two_sample_t_test(sample_a, sample_b)
    assert t_stat > 10.0
    assert p_val < 0.01

    # Identical samples
    t_stat_same, p_val_same = two_sample_t_test(sample_a, sample_a)
    assert t_stat_same == 0.0
    assert p_val_same == 1.0


def test_learning_curve_stats_and_ci_gate() -> None:
    """Test multi-phase learning curve evaluation and CI gating rules."""
    # Phase A: 5 runs, average ~30s, llm_fresh, no runbook
    results_a = [
        ChaosResult(
            scenario="pod_kill",
            recovery_time_s=30.0,
            cost_usd=0.05,
            tier_used="llm_fresh",
            success=True,
        )
        for _ in range(5)
    ]

    # Phase B: 10 runs, progressing from 30s down to 5s, runbook used with 100% precision
    results_b = [
        ChaosResult(
            scenario="pod_kill",
            recovery_time_s=28.0 if i == 0 else 10.0 if i < 4 else 5.0,
            cost_usd=0.05 if i == 0 else 0.01 if i < 4 else 0.0,
            tier_used="llm_fresh" if i == 0 else "runbook" if i < 4 else "tier0",
            runbook_used=i > 0,
            runbook_was_correct=i > 0,
            success=True,
        )
        for i in range(10)
    ]

    # Phase C: 3 runs, regression back to ~29s when memory wiped
    results_c = [
        ChaosResult(
            scenario="pod_kill",
            recovery_time_s=29.0,
            cost_usd=0.05,
            tier_used="llm_fresh",
            success=True,
        )
        for _ in range(3)
    ]

    stats = calculate_learning_curve_stats(results_a, results_b, results_c, scenario_name="pod_kill")

    assert stats.scenario == "pod_kill"
    assert stats.mttr_reduction_pct > 70.0
    assert stats.cost_reduction_pct > 80.0
    assert stats.is_significant is True
    assert stats.ablation_regressed is True
    assert stats.precision_target_met is True
    assert stats.safety_target_met is True
    assert stats.passed_ci_gate is True
    assert len(stats.failure_reasons) == 0


def test_plot_generation(tmp_path: Path) -> None:
    """Test generating visual plot PNG from experimental results."""
    plot_file = tmp_path / "test_learning_curve.png"

    results_a = [
        ChaosResult(scenario="disk_fill", recovery_time_s=25.0, cost_usd=0.04, tier_used="llm_fresh", success=True)
        for _ in range(3)
    ]
    results_b = [
        ChaosResult(scenario="disk_fill", recovery_time_s=8.0, cost_usd=0.01, tier_used="runbook", success=True)
        for _ in range(5)
    ]
    results_c = [
        ChaosResult(scenario="disk_fill", recovery_time_s=24.0, cost_usd=0.04, tier_used="llm_fresh", success=True)
        for _ in range(2)
    ]

    stats = calculate_learning_curve_stats(results_a, results_b, results_c, scenario_name="disk_fill")
    saved_path = plot_learning_curves(stats, results_a, results_b, results_c, output_path=plot_file)

    assert saved_path.exists()
    assert saved_path.stat().st_size > 1000  # Generated valid non-empty PNG


def test_experiment_orchestration_simulated(tmp_path: Path) -> None:
    """Test full 3-phase experiment orchestration in simulated mode."""
    scenario = ChaosScenario(
        name="test_sim_scenario",
        category="pod",
        description="Simulated scenario for learning curve",
        attack=lambda: None,
    )

    wiped_count = 0

    def mock_wipe() -> None:
        nonlocal wiped_count
        wiped_count += 1

    experiment = LearningCurveExperiment(
        scenario=scenario,
        runs_a=3,
        runs_b=6,
        runs_c=2,
        wipe_memory_fn=mock_wipe,
        output_dir=tmp_path,
        simulate=True,
    )

    stats, res_a, res_b, res_c = experiment.run_experiment()

    # Memory wiped twice: before Phase A, and before Phase C
    assert wiped_count == 2
    assert len(res_a) == 3
    assert len(res_b) == 6
    assert len(res_c) == 2

    assert stats.passed_ci_gate is True
    assert stats.mttr_reduction_pct > 50.0

    # Ensure artifacts generated
    report_file = tmp_path / "test_sim_scenario_learning_curve_report.md"
    json_file = tmp_path / "test_sim_scenario_learning_curve.json"
    plot_file = tmp_path / "test_sim_scenario_learning_curve.png"

    assert report_file.exists()
    assert json_file.exists()
    assert plot_file.exists()

    report_content = report_file.read_text(encoding="utf-8")
    assert "Learning Curve & Memory Ablation Report" in report_content
    assert "PASSED" in report_content
