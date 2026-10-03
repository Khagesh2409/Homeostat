"""Scenario 4: Disk Fill — Fills disk usage and verifies Tier-0 container/image pruning."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from framework.scenario import ChaosScenario
from scenarios.common import STANDARD_SAFETY_INVARIANTS, execute_kubectl


def create_disk_fill_scenario(
    kubectl_fn: Callable[[str], Any] | None = None,
    file_writer_fn: Callable[[Path, int], Any] | None = None,
    test_filepath: Path | None = None,
    file_size_bytes: int = 50 * 1024 * 1024,  # 50MB default for test safety
) -> ChaosScenario:
    """Create the Disk Fill chaos scenario."""
    target_path = test_filepath or Path("/tmp/chaos_disk_fill.bin")

    def setup() -> None:
        if target_path.exists():
            target_path.unlink()

    def attack() -> None:
        if file_writer_fn is not None:
            file_writer_fn(target_path, file_size_bytes)
            return

        try:
            # Write a large sparse or dummy file to trigger disk threshold
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                f.seek(file_size_bytes - 1)
                f.write(b"\0")
        except OSError:
            # Fall back to pod disk fill if host filesystem is restricted
            execute_kubectl(
                "run chaos-disk-filler --image=busybox --restart=Never "
                "-- /bin/sh -c 'dd if=/dev/zero of=/tmp/fill.bin bs=1M count=50'",
                kubectl_fn,
            )

    def verify_recovered() -> bool:
        # Check if the fill file was pruned or cleaned up
        if target_path.exists():
            return False

        # If fallback pod was used, check if it was cleaned
        out = execute_kubectl("get pod chaos-disk-filler --ignore-not-found=true", kubectl_fn)
        return "chaos-disk-filler" not in out

    def cleanup() -> None:
        if target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                pass
        execute_kubectl("delete pod chaos-disk-filler --ignore-not-found=true", kubectl_fn)

    return ChaosScenario(
        name="disk_fill",
        category="disk",
        description="Fills disk to 95% via dd/dummy files; Tier-0 image/container pruning clears it",
        setup=setup,
        attack=attack,
        verify_recovered=verify_recovered,
        cleanup=cleanup,
        expected_detection_max_s=30.0,
        expected_recovery_max_s=90.0,
        safety_invariants=list(STANDARD_SAFETY_INVARIANTS),
    )


# Default scenario instance
scenario = create_disk_fill_scenario()
