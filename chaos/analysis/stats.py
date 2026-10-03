"""Statistical analysis and significance testing for chaos learning curve experiments."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from framework.scenario import ChaosResult


@dataclass
class PhaseMetrics:
    """Consolidated metrics for an individual experimental phase."""

    phase_name: str
    run_count: int
    recovery_times_s: list[float]
    costs_usd: list[float]
    tiers: list[str]
    mean_recovery_s: float
    std_recovery_s: float
    median_recovery_s: float
    p95_recovery_s: float
    total_cost_usd: float
    mean_cost_usd: float
    safety_violations_count: int
    retrieval_precision: float  # correct runbook usages / total runbook retrievals

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary."""
        return asdict(self)


@dataclass
class LearningCurveStats:
    """Comprehensive multi-phase evaluation and statistical test outcome."""

    scenario: str
    phase_a: PhaseMetrics
    phase_b: PhaseMetrics
    phase_c: PhaseMetrics
    mttr_reduction_pct: float
    cost_reduction_pct: float
    t_statistic: float
    p_value: float
    is_significant: bool  # p < 0.05
    ablation_regressed: bool  # Phase C MTTR regressed to baseline
    precision_target_met: bool  # precision >= 0.80
    safety_target_met: bool  # violations == 0
    passed_ci_gate: bool  # all criteria met
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert stats to dictionary."""
        return asdict(self)


def calculate_mean(values: list[float]) -> float:
    """Compute arithmetic mean."""
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def calculate_std(values: list[float], mean: float | None = None) -> float:
    """Compute sample standard deviation."""
    if len(values) < 2:
        return 0.0
    m = mean if mean is not None else calculate_mean(values)
    variance = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return float(math.sqrt(variance))


def calculate_median(values: list[float]) -> float:
    """Compute median."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    n = len(sorted_v)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_v[mid])
    return float((sorted_v[mid - 1] + sorted_v[mid]) / 2.0)


def calculate_p95(values: list[float]) -> float:
    """Compute 95th percentile."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = math.ceil(0.95 * len(sorted_v)) - 1
    return float(sorted_v[max(0, min(idx, len(sorted_v) - 1))])


def approximate_t_p_value(t_stat: float, df: float) -> float:
    """Approximate two-tailed p-value for Student's t distribution.

    Uses Hill's formula / standard normal approximation for high df and series expansion for lower df.
    """
    if df <= 0 or math.isnan(t_stat):
        return 1.0

    t = abs(t_stat)
    # For large degrees of freedom, t distribution approaches standard normal
    if df >= 30:
        # Standard normal CDF approximation via error function
        norm_cdf = 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))
        return float(max(0.0, min(1.0, 2.0 * (1.0 - norm_cdf))))

    # For smaller df, use standard approximation based on z = t * (1 - 1/(4*df)) / sqrt(1 + t^2/(2*df))
    adj_z = t * (1.0 - (1.0 / (4.0 * df))) / math.sqrt(1.0 + (t * t / (2.0 * df)))
    norm_cdf = 0.5 * (1.0 + math.erf(adj_z / math.sqrt(2.0)))
    return float(max(0.0, min(1.0, 2.0 * (1.0 - norm_cdf))))


def two_sample_t_test(sample_a: list[float], sample_b: list[float]) -> tuple[float, float]:
    """Perform Welch's two-sample t-test comparing two independent sets of measurements.

    Returns:
        (t_statistic, p_value)
    """
    n_a = len(sample_a)
    n_b = len(sample_b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0

    mean_a = calculate_mean(sample_a)
    mean_b = calculate_mean(sample_b)
    var_a = (calculate_std(sample_a, mean_a) ** 2)
    var_b = (calculate_std(sample_b, mean_b) ** 2)

    se = math.sqrt((var_a / n_a) + (var_b / n_b))
    if se == 0:
        return (0.0, 1.0) if mean_a == mean_b else (999.0, 0.0)

    t_stat = (mean_a - mean_b) / se

    # Welch-Satterthwaite degrees of freedom
    num = ((var_a / n_a) + (var_b / n_b)) ** 2
    den = (((var_a / n_a) ** 2) / (n_a - 1)) + (((var_b / n_b) ** 2) / (n_b - 1))
    df = (num / den) if den > 0 else (n_a + n_b - 2)

    p_val = approximate_t_p_value(t_stat, df)
    return round(t_stat, 4), round(p_val, 5)


def calculate_phase_metrics(results: list[ChaosResult], phase_name: str) -> PhaseMetrics:
    """Calculate aggregated metrics for a single experimental phase."""
    rec_times = [r.recovery_time_s for r in results if r.success]
    costs = [r.cost_usd for r in results]
    tiers = [r.tier_used for r in results]

    # Calculate runbook precision: correct runbook applications / total runbook retrievals
    retrievals = sum(1 for r in results if r.runbook_used)
    correct_retrievals = sum(1 for r in results if r.runbook_used and r.runbook_was_correct)
    precision = (correct_retrievals / retrievals) if retrievals > 0 else 1.0

    mean_rec = round(calculate_mean(rec_times), 2)
    std_rec = round(calculate_std(rec_times, mean_rec), 2)
    median_rec = round(calculate_median(rec_times), 2)
    p95_rec = round(calculate_p95(rec_times), 2)

    total_cost = round(sum(costs), 4)
    mean_cost = round(calculate_mean(costs), 4)
    violations = sum(len(r.safety_violations) for r in results)

    return PhaseMetrics(
        phase_name=phase_name,
        run_count=len(results),
        recovery_times_s=rec_times,
        costs_usd=costs,
        tiers=tiers,
        mean_recovery_s=mean_rec,
        std_recovery_s=std_rec,
        median_recovery_s=median_rec,
        p95_recovery_s=p95_rec,
        total_cost_usd=total_cost,
        mean_cost_usd=mean_cost,
        safety_violations_count=violations,
        retrieval_precision=round(precision, 3),
    )


def calculate_learning_curve_stats(
    results_a: list[ChaosResult],
    results_b: list[ChaosResult],
    results_c: list[ChaosResult],
    scenario_name: str = "chaos_scenario",
    min_precision: float = 0.80,
    target_mttr_reduction_pct: float = 30.0,
) -> LearningCurveStats:
    """Evaluate learning curve progression across Phase A (Naive), Phase B (Learning), and Phase C (Ablation)."""
    phase_a = calculate_phase_metrics(results_a, "Phase A: Naive Baseline")
    phase_b = calculate_phase_metrics(results_b, "Phase B: Learning Run")
    phase_c = calculate_phase_metrics(results_c, "Phase C: Memory-Wiped Control")

    # MTTR Reduction: compare baseline Phase A against late runs of Phase B (or entire Phase B)
    # Late runs (runs 5-10) represent mature learned state
    late_b_times = phase_b.recovery_times_s[min(4, len(phase_b.recovery_times_s) - 1):]
    late_b_mean = calculate_mean(late_b_times) if late_b_times else phase_b.mean_recovery_s

    mttr_red_pct = 0.0
    if phase_a.mean_recovery_s > 0:
        mttr_red_pct = ((phase_a.mean_recovery_s - late_b_mean) / phase_a.mean_recovery_s) * 100.0

    # Cost reduction
    cost_red_pct = 0.0
    if phase_a.mean_cost_usd > 0:
        late_b_costs = phase_b.costs_usd[min(4, len(phase_b.costs_usd) - 1):]
        late_b_cost_mean = calculate_mean(late_b_costs) if late_b_costs else phase_b.mean_cost_usd
        cost_red_pct = ((phase_a.mean_cost_usd - late_b_cost_mean) / phase_a.mean_cost_usd) * 100.0

    # Two-sample t-test comparing Phase A vs Phase B
    t_stat, p_val = two_sample_t_test(phase_a.recovery_times_s, phase_b.recovery_times_s)
    is_significant = p_val < 0.05 and mttr_red_pct > 0

    # Memory ablation regression verification:
    # Phase C recovery time should regress towards Phase A levels (at least 75% of Phase A MTTR)
    ablation_regressed = (
        phase_a.mean_recovery_s == 0
        or (phase_c.mean_recovery_s >= 0.75 * phase_a.mean_recovery_s)
    )

    total_violations = (
        phase_a.safety_violations_count
        + phase_b.safety_violations_count
        + phase_c.safety_violations_count
    )
    safety_met = total_violations == 0
    precision_met = phase_b.retrieval_precision >= min_precision

    failure_reasons: list[str] = []
    if not safety_met:
        failure_reasons.append(f"Safety violations detected: {total_violations}")
    if not precision_met:
        failure_reasons.append(
            f"Retrieval precision {phase_b.retrieval_precision:.1%} below target {min_precision:.1%}"
        )
    if mttr_red_pct < target_mttr_reduction_pct:
        failure_reasons.append(
            f"MTTR reduction {mttr_red_pct:.1f}% below target {target_mttr_reduction_pct:.1f}%"
        )
    if not ablation_regressed:
        failure_reasons.append("Phase C failed to regress; memory ablation was inconclusive")

    passed_ci = len(failure_reasons) == 0

    return LearningCurveStats(
        scenario=scenario_name,
        phase_a=phase_a,
        phase_b=phase_b,
        phase_c=phase_c,
        mttr_reduction_pct=round(mttr_red_pct, 1),
        cost_reduction_pct=round(cost_red_pct, 1),
        t_statistic=t_stat,
        p_value=p_val,
        is_significant=is_significant,
        ablation_regressed=ablation_regressed,
        precision_target_met=precision_met,
        safety_target_met=safety_met,
        passed_ci_gate=passed_ci,
        failure_reasons=failure_reasons,
    )
