"""Recorder for storing chaos experiment results locally and in S3."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import boto3
import structlog
from botocore.exceptions import ClientError

from framework.scenario import ChaosResult

logger = structlog.get_logger(__name__)

DEFAULT_LOCAL_RESULTS_FILE = Path("results/chaos_runs.jsonl")


class ResultRecorder:
    """Records and analyzes chaos testing results locally and in AWS S3."""

    def __init__(
        self,
        local_path: Path | str = DEFAULT_LOCAL_RESULTS_FILE,
        s3_bucket: str = "",
        s3_prefix: str = "chaos-results",
        s3_client: Any | None = None,
    ) -> None:
        self.local_path = Path(local_path)
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix.strip("/")
        self._s3 = s3_client or (boto3.client("s3") if s3_bucket else None)

    def record(self, result: ChaosResult) -> str:
        """Save result to local JSONL and optionally upload to S3."""
        # 1. Local append
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(result.to_dict()) + "\n"
        with open(self.local_path, "a", encoding="utf-8") as f:
            f.write(line)

        logger.info(
            "chaos_result_recorded_locally",
            scenario=result.scenario,
            run_id=result.run_id,
            success=result.success,
        )

        # 2. Upload to S3 if configured
        if self._s3 and self.s3_bucket:
            s3_key = f"{self.s3_prefix}/{result.scenario}/{result.run_id}.json"
            try:
                self._s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=s3_key,
                    Body=json.dumps(result.to_dict(), indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
                logger.info(
                    "chaos_result_uploaded_to_s3",
                    bucket=self.s3_bucket,
                    key=s3_key,
                )
            except ClientError as e:
                logger.warning(
                    "s3_upload_failed",
                    error=str(e),
                    scenario=result.scenario,
                )

        return result.run_id

    def load_results(self, scenario: str | None = None) -> list[ChaosResult]:
        """Load recorded results from local storage."""
        if not self.local_path.exists():
            return []

        results: list[ChaosResult] = []
        with open(self.local_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    res = ChaosResult.from_dict(data)
                    if scenario is None or res.scenario == scenario:
                        results.append(res)
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    logger.warning("failed_to_parse_chaos_line", error=str(exc))

        return results

    def calculate_learning_curve(self) -> dict[str, Any]:
        """Aggregate metrics over successive runs to calculate MTTR learning curves."""
        all_results = self.load_results()
        grouped: dict[str, list[ChaosResult]] = defaultdict(list)
        for res in all_results:
            grouped[res.scenario].append(res)

        summary: dict[str, Any] = {}
        for scen_name, runs in grouped.items():
            # Sort chronologically
            sorted_runs = sorted(runs, key=lambda r: r.timestamp)
            recovery_times = [r.recovery_time_s for r in sorted_runs if r.success]
            costs = [r.cost_usd for r in sorted_runs]
            tier_distribution: dict[str, int] = defaultdict(int)
            for r in sorted_runs:
                tier_distribution[r.tier_used] += 1

            initial_mttr = recovery_times[0] if recovery_times else 0.0
            latest_mttr = recovery_times[-1] if recovery_times else 0.0
            reduction_pct = 0.0
            if initial_mttr > 0 and latest_mttr < initial_mttr:
                reduction_pct = ((initial_mttr - latest_mttr) / initial_mttr) * 100.0

            summary[scen_name] = {
                "total_runs": len(sorted_runs),
                "successful_runs": sum(1 for r in sorted_runs if r.success),
                "initial_recovery_s": initial_mttr,
                "latest_recovery_s": latest_mttr,
                "mttr_reduction_pct": round(reduction_pct, 1),
                "total_cost_usd": round(sum(costs), 4),
                "tier_distribution": dict(tier_distribution),
            }

        return summary
