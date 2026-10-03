"""Orchestrates 3-phase chaos learning curve measurement and memory ablation experiments."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import structlog

from analysis.plot import plot_learning_curves
from analysis.stats import LearningCurveStats, calculate_learning_curve_stats
from framework.runner import ScenarioRunner
from framework.scenario import ChaosResult, ChaosScenario
from scenarios import SCENARIOS_MAP

logger = structlog.get_logger(__name__)


class LearningCurveExperiment:
    """Orchestrates the 3-phase learning curve evaluation: Naive (A), Learning (B), and Ablation (C)."""

    def __init__(
        self,
        scenario: ChaosScenario,
        runner: ScenarioRunner | None = None,
        runs_a: int = 5,
        runs_b: int = 10,
        runs_c: int = 3,
        wipe_memory_fn: Callable[[], Any] | None = None,
        agent_url: str = "http://localhost:8080",
        watchdog_url: str = "http://localhost:8000",
        output_dir: Path | str = "chaos/reports",
        simulate: bool = False,
    ) -> None:
        self.scenario = scenario
        self.runner = runner or ScenarioRunner(agent_url=agent_url, watchdog_url=watchdog_url)
        self.runs_a = runs_a
        self.runs_b = runs_b
        self.runs_c = runs_c
        self.wipe_memory_fn = wipe_memory_fn
        self.agent_url = agent_url.rstrip("/")
        self.watchdog_url = watchdog_url.rstrip("/")
        self.output_dir = Path(output_dir)
        self.simulate = simulate
        self._http = httpx.Client(timeout=5.0)

    def wipe_memory(self) -> None:
        """Clear the agent's persistent memory and runbook cache."""
        if self.wipe_memory_fn is not None:
            self.wipe_memory_fn()
            return

        try:
            resp = self._http.post(f"{self.agent_url}/admin/reset-memory")
            if resp.status_code == 200:
                logger.info("memory_store_wiped_via_api")
        except (httpx.HTTPError, OSError):
            logger.debug("memory_wipe_api_unavailable")

    def _execute_run(self, phase_name: str, run_index: int, total_runs: int) -> ChaosResult:
        """Execute a single scenario run or generate simulated outcome."""
        if self.simulate:
            # Simulate realistic progression:
            # Phase A: slow manual/LLM (e.g., 25-35s, cost $0.05, llm_fresh)
            # Phase B: run 1 = 30s ($0.05), runs 2-4 = 12s ($0.01, runbook), runs 5-10 = 5s ($0.00, tier0)
            # Phase C: regression back to 25-35s ($0.05, llm_fresh)
            if "Phase A" in phase_name:
                rec_time = 28.0 + (run_index % 3) * 1.5
                cost = 0.045
                tier = "llm_fresh"
                used_runbook = False
                correct_runbook = False
            elif "Phase B" in phase_name:
                if run_index == 1:
                    rec_time = 29.0
                    cost = 0.048
                    tier = "llm_fresh"
                    used_runbook = False
                    correct_runbook = False
                elif run_index <= 4:
                    rec_time = 12.0 - (run_index * 0.8)
                    cost = 0.012
                    tier = "runbook"
                    used_runbook = True
                    correct_runbook = True
                else:
                    rec_time = 5.0 + (run_index % 2) * 0.5
                    cost = 0.000
                    tier = "tier0"
                    used_runbook = True
                    correct_runbook = True
            else:  # Phase C (Ablation)
                rec_time = 27.5 + (run_index % 3) * 2.0
                cost = 0.046
                tier = "llm_fresh"
                used_runbook = False
                correct_runbook = False

            return ChaosResult(
                scenario=self.scenario.name,
                detection_time_s=4.0,
                recovery_time_s=round(rec_time, 2),
                total_time_s=round(rec_time + 4.0, 2),
                cost_usd=cost,
                tier_used=tier,
                runbook_used=used_runbook,
                runbook_was_correct=correct_runbook,
                success=True,
                safety_violations=[],
            )

        # Real execution via ScenarioRunner
        return self.runner.run_scenario(self.scenario)

    def run_phase_a(self) -> list[ChaosResult]:
        """Phase A: Naive baseline — Run scenario N times with empty runbook store."""
        logger.info("starting_phase_a_naive_baseline", scenario=self.scenario.name, runs=self.runs_a)
        self.wipe_memory()
        results: list[ChaosResult] = []
        for i in range(1, self.runs_a + 1):
            res = self._execute_run("Phase A", i, self.runs_a)
            results.append(res)
        return results

    def run_phase_b(self) -> list[ChaosResult]:
        """Phase B: Learning run — Run scenario N times with persistent runbook store."""
        logger.info("starting_phase_b_learning", scenario=self.scenario.name, runs=self.runs_b)
        results: list[ChaosResult] = []
        for i in range(1, self.runs_b + 1):
            res = self._execute_run("Phase B", i, self.runs_b)
            results.append(res)
        return results

    def run_phase_c(self) -> list[ChaosResult]:
        """Phase C: Memory-wiped control — Wipe memory and verify regression."""
        logger.info("starting_phase_c_ablation", scenario=self.scenario.name, runs=self.runs_c)
        self.wipe_memory()
        results: list[ChaosResult] = []
        for i in range(1, self.runs_c + 1):
            res = self._execute_run("Phase C", i, self.runs_c)
            results.append(res)
        return results

    def generate_markdown_report(
        self,
        stats: LearningCurveStats,
        plot_path: Path,
    ) -> Path:
        """Produce formatted markdown evaluation report."""
        report_file = self.output_dir / f"{self.scenario.name}_learning_curve_report.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Learning Curve & Memory Ablation Report: `{stats.scenario}`",
            "",
            f"**Evaluation Date:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  ",
            f"**CI Gate Status:** {'PASSED' if stats.passed_ci_gate else 'FAILED'}  ",
            f"**Statistical Significance:** p={stats.p_value:.5f} (t={stats.t_statistic:.2f})  ",
            "",
            "---",
            "",
            "## 1. Summary Findings",
            "",
            f"- **MTTR Reduction:** {stats.mttr_reduction_pct:.1f}% improvement from naive baseline to learned runbook state.",
            f"- **Cost Reduction:** {stats.cost_reduction_pct:.1f}% reduction in Bedrock token costs.",
            f"- **Retrieval Precision:** {stats.phase_b.retrieval_precision:.1%} (Target >= 80%).",
            f"- **Ablation Regression:** {'Confirmed' if stats.ablation_regressed else 'Failed'} (MTTR regressed back to naive levels when memory wiped).",
            f"- **Safety Invariant Violations:** {stats.phase_a.safety_violations_count + stats.phase_b.safety_violations_count + stats.phase_c.safety_violations_count} (Target: 0).",
            "",
            "---",
            "",
            "## 2. Phase Comparison Metrics",
            "",
            "| Metric | Phase A (Naive) | Phase B (Learning) | Phase C (Ablation) |",
            "|---|---|---|---|",
            f"| **Runs** | {stats.phase_a.run_count} | {stats.phase_b.run_count} | {stats.phase_c.run_count} |",
            f"| **Mean Recovery Time (s)** | {stats.phase_a.mean_recovery_s:.2f}s | {stats.phase_b.mean_recovery_s:.2f}s | {stats.phase_c.mean_recovery_s:.2f}s |",
            f"| **Median Recovery Time (s)** | {stats.phase_a.median_recovery_s:.2f}s | {stats.phase_b.median_recovery_s:.2f}s | {stats.phase_c.median_recovery_s:.2f}s |",
            f"| **p95 Recovery Time (s)** | {stats.phase_a.p95_recovery_s:.2f}s | {stats.phase_b.p95_recovery_s:.2f}s | {stats.phase_c.p95_recovery_s:.2f}s |",
            f"| **Mean Cost ($ USD)** | ${stats.phase_a.mean_cost_usd:.4f} | ${stats.phase_b.mean_cost_usd:.4f} | ${stats.phase_c.mean_cost_usd:.4f} |",
            f"| **Total Cost ($ USD)** | ${stats.phase_a.total_cost_usd:.4f} | ${stats.phase_b.total_cost_usd:.4f} | ${stats.phase_c.total_cost_usd:.4f} |",
            f"| **Runbook Precision** | N/A | {stats.phase_b.retrieval_precision:.1%} | N/A |",
            f"| **Safety Violations** | {stats.phase_a.safety_violations_count} | {stats.phase_b.safety_violations_count} | {stats.phase_c.safety_violations_count} |",
            "",
            "---",
            "",
            "## 3. Visual Learning Curve",
            "",
            f"![Learning Curve]({plot_path.name})",
            "",
            "---",
            "",
            "## 4. CI Gate Decision",
            "",
        ]

        if stats.passed_ci_gate:
            lines.append(" **ALL CI CRITERIA SATISFIED**: Performance improved significantly with verified memory causality.")
        else:
            lines.append(" **CI GATE FAILED**: Identified issues:")
            for reason in stats.failure_reasons:
                lines.append(f"- {reason}")

        report_file.write_text("\n".join(lines), encoding="utf-8")
        return report_file

    def run_experiment(self) -> tuple[LearningCurveStats, list[ChaosResult], list[ChaosResult], list[ChaosResult]]:
        """Execute all 3 phases, perform statistical analysis, generate plots, and write markdown report."""
        res_a = self.run_phase_a()
        res_b = self.run_phase_b()
        res_c = self.run_phase_c()

        stats = calculate_learning_curve_stats(
            res_a,
            res_b,
            res_c,
            scenario_name=self.scenario.name,
        )

        # Plot learning curves
        plot_file = self.output_dir / f"{self.scenario.name}_learning_curve.png"
        plot_learning_curves(stats, res_a, res_b, res_c, output_path=plot_file)

        # Generate markdown report
        self.generate_markdown_report(stats, plot_file)

        # Save JSON results
        results_data = {
            "scenario": self.scenario.name,
            "stats": stats.to_dict(),
            "phase_a_results": [r.to_dict() for r in res_a],
            "phase_b_results": [r.to_dict() for r in res_b],
            "phase_c_results": [r.to_dict() for r in res_c],
        }
        json_file = self.output_dir / f"{self.scenario.name}_learning_curve.json"
        json_file.write_text(json.dumps(results_data, indent=2), encoding="utf-8")

        return stats, res_a, res_b, res_c


def main() -> None:
    """CLI entry point for running learning curve measurements."""
    parser = argparse.ArgumentParser(description="Homeostat Learning Curve Measurement Harness")
    parser.add_argument("--scenario", type=str, default="pod_kill", help="Scenario to evaluate")
    parser.add_argument("--runs-a", type=int, default=5, help="Number of baseline runs for Phase A")
    parser.add_argument("--runs-b", type=int, default=10, help="Number of learning runs for Phase B")
    parser.add_argument("--runs-c", type=int, default=3, help="Number of ablation runs for Phase C")
    parser.add_argument("--agent-url", type=str, default="http://localhost:8080", help="Agent base URL")
    parser.add_argument("--watchdog-url", type=str, default="http://localhost:8000", help="Watchdog base URL")
    parser.add_argument("--output-dir", type=str, default="chaos/reports", help="Directory for reports and plots")
    parser.add_argument("--simulate", action="store_true", help="Simulate execution without cluster")
    args = parser.parse_args()

    if args.scenario not in SCENARIOS_MAP:
        print(f"Error: Unknown scenario '{args.scenario}'. Available: {list(SCENARIOS_MAP.keys())}")
        sys.exit(1)

    scenario = SCENARIOS_MAP[args.scenario]
    experiment = LearningCurveExperiment(
        scenario=scenario,
        runs_a=args.runs_a,
        runs_b=args.runs_b,
        runs_c=args.runs_c,
        agent_url=args.agent_url,
        watchdog_url=args.watchdog_url,
        output_dir=args.output_dir,
        simulate=args.simulate,
    )

    stats, _, _, _ = experiment.run_experiment()
    print(f"Experiment completed for '{args.scenario}':")
    print(f"  MTTR Reduction: {stats.mttr_reduction_pct:.1f}%")
    print(f"  Cost Reduction: {stats.cost_reduction_pct:.1f}%")
    print(f"  Precision: {stats.phase_b.retrieval_precision:.1%}")
    print(f"  p-value: {stats.p_value:.5f} (significant: {stats.is_significant})")
    print(f"  CI Gate Passed: {stats.passed_ci_gate}")

    if not stats.passed_ci_gate:
        sys.exit(1)


if __name__ == "__main__":
    main()
