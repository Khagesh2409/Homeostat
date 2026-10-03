"""Visualization and learning curve plot generator for chaos engineering experiments."""

from __future__ import annotations

from pathlib import Path

import matplotlib

# Use Agg backend for headless / non-GUI environments
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analysis.stats import LearningCurveStats
from framework.scenario import ChaosResult

TIER_COLORS = {
    "llm_fresh": "#e74c3c",  # Red
    "runbook": "#3498db",    # Blue
    "tier0": "#2ecc71",      # Green
}


def plot_learning_curves(
    stats: LearningCurveStats,
    results_a: list[ChaosResult],
    results_b: list[ChaosResult],
    results_c: list[ChaosResult],
    output_path: Path | str = "chaos/reports/learning_curve.png",
) -> Path:
    """Generate a multi-panel visual report of the learning curve and memory ablation experiment."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax_time, ax_cost) = plt.subplots(2, 1, figsize=(11, 8), sharex=True)

    all_results = results_a + results_b + results_c
    run_indices = list(range(1, len(all_results) + 1))
    recovery_times = [r.recovery_time_s for r in all_results]
    costs = [r.cost_usd for r in all_results]
    tiers = [r.tier_used for r in all_results]

    n_a = len(results_a)
    n_b = len(results_b)

    # ── Panel 1: Recovery Time Curve ──────────────────────────────────────────
    ax_time.plot(run_indices, recovery_times, color="#7f8c8d", linestyle="--", alpha=0.6, zorder=1)

    for idx, (x, y, tier) in enumerate(zip(run_indices, recovery_times, tiers, strict=False)):
        color = TIER_COLORS.get(tier, "#95a5a6")
        ax_time.scatter(x, y, color=color, s=70, zorder=2, label=tier if idx == 0 else "")

    # Phase dividers
    ax_time.axvline(x=n_a + 0.5, color="#e67e22", linestyle=":", linewidth=2, label="Phase Divider")
    ax_time.axvline(x=n_a + n_b + 0.5, color="#e67e22", linestyle=":", linewidth=2)

    ax_time.set_ylabel("Recovery Time (s)", fontsize=11, fontweight="bold")
    ax_time.set_title(
        f"Homeostat Learning Curve: {stats.scenario.upper()} "
        f"(MTTR Reduction: {stats.mttr_reduction_pct:.1f}%, Cost Reduction: {stats.cost_reduction_pct:.1f}%)",
        fontsize=13,
        fontweight="bold",
    )
    ax_time.grid(True, linestyle=":", alpha=0.6)

    # Add phase text annotations
    max_time = max(recovery_times) if recovery_times else 10.0
    ax_time.text((n_a + 1) / 2, max_time * 0.9, "Phase A: Naive\n(Empty Memory)", ha="center", fontsize=9, weight="bold")
    ax_time.text(n_a + (n_b + 1) / 2, max_time * 0.9, "Phase B: Learning\n(Persistent Memory)", ha="center", fontsize=9, weight="bold")
    ax_time.text(n_a + n_b + (len(results_c) + 1) / 2, max_time * 0.9, "Phase C: Ablation\n(Memory Wiped)", ha="center", fontsize=9, weight="bold")

    # ── Panel 2: Cost Per Incident ────────────────────────────────────────────
    ax_cost.plot(run_indices, costs, color="#2c3e50", marker="s", markersize=6, linewidth=1.5)
    ax_cost.axvline(x=n_a + 0.5, color="#e67e22", linestyle=":", linewidth=2)
    ax_cost.axvline(x=n_a + n_b + 0.5, color="#e67e22", linestyle=":", linewidth=2)

    ax_cost.set_xlabel("Cumulative Incident Run Number", fontsize=11, fontweight="bold")
    ax_cost.set_ylabel("Bedrock Cost ($ USD)", fontsize=11, fontweight="bold")
    ax_cost.grid(True, linestyle=":", alpha=0.6)

    # Custom legend for tiers
    handles = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=TIER_COLORS["llm_fresh"], markersize=8, label="Tier: LLM Fresh"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=TIER_COLORS["runbook"], markersize=8, label="Tier: Runbook"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=TIER_COLORS["tier0"], markersize=8, label="Tier: Tier-0 Playbook"),
    ]
    ax_time.legend(handles=handles, loc="upper right")

    plt.tight_layout()
    fig.savefig(out_file, dpi=150)
    plt.close(fig)

    return out_file
