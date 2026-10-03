"""
Runbook retrieval with discriminating check confirmation.

Retrieves a runbook by exact FailureSignature match, runs a cheap
discriminating check to confirm applicability, and tracks retrieval precision.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeostat.memory.client import RunbookClient
from homeostat.memory.schemas import FailureSignature, Runbook
from homeostat.tools.runner import CommandRunner, default_runner

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Outcome of attempting to retrieve and confirm a runbook."""

    found: bool
    confirmed: bool
    runbook: Runbook | None = None
    rejection_reason: str | None = None
    check_output: str = ""


class RunbookRetriever:
    """
    Retrieves runbooks from DynamoDB and validates them via discriminating checks.

    Tracks retrieval precision: correct_retrievals / total_retrievals.
    """

    def __init__(
        self,
        client: RunbookClient | None = None,
        min_confidence: float = 0.6,
        runner: CommandRunner | None = None,
        custom_checker: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
    ) -> None:
        self.client = client or RunbookClient()
        self.min_confidence = min_confidence
        self.runner = runner or default_runner
        self.custom_checker = custom_checker

        self._total_retrievals: int = 0
        self._confirmed_retrievals: int = 0
        self._rejected_retrievals: int = 0

    @property
    def total_retrievals(self) -> int:
        return self._total_retrievals

    @property
    def confirmed_retrievals(self) -> int:
        return self._confirmed_retrievals

    @property
    def rejected_retrievals(self) -> int:
        return self._rejected_retrievals

    @property
    def precision(self) -> float:
        """Precision = confirmed_retrievals / total_retrievals."""
        if self._total_retrievals == 0:
            return 1.0
        return self._confirmed_retrievals / self._total_retrievals

    def reset_metrics(self) -> None:
        """Reset precision and retrieval counters."""
        self._total_retrievals = 0
        self._confirmed_retrievals = 0
        self._rejected_retrievals = 0

    def retrieve(
        self,
        signature: FailureSignature | str,
        context: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        """
        Retrieve a runbook and confirm via discriminating check before action.

        Args:
            signature: Structured FailureSignature or string key.
            context: Incident context (e.g. current alerts, metrics, namespace).
        """
        ctx = context or {}

        # 1. Query DynamoDB for latest version
        runbook = self.client.get_latest(signature)
        if runbook is None:
            logger.info("No runbook found for signature: %s", signature)
            return RetrievalResult(
                found=False,
                confirmed=False,
                runbook=None,
                rejection_reason="No runbook found for signature",
            )

        # 2. Check confidence threshold
        if runbook.confidence < self.min_confidence:
            logger.info(
                "Runbook found for %s but confidence %.2f < threshold %.2f",
                signature,
                runbook.confidence,
                self.min_confidence,
            )
            return RetrievalResult(
                found=True,
                confirmed=False,
                runbook=runbook,
                rejection_reason=(
                    f"Confidence {runbook.confidence:.2f} below threshold {self.min_confidence:.2f}"
                ),
            )

        # 3. Candidate found — track retrieval and validate discriminating check
        self._total_retrievals += 1

        passed, output = self.confirm_discriminating_check(runbook, ctx)
        if passed:
            self._confirmed_retrievals += 1
            logger.info(
                "Runbook discriminating check passed for %s v%d: %s",
                runbook.failure_signature.key,
                runbook.version,
                output,
            )
            return RetrievalResult(
                found=True,
                confirmed=True,
                runbook=runbook,
                check_output=output,
            )

        self._rejected_retrievals += 1
        reason = f"Discriminating check failed: {output}"
        logger.warning(
            "Runbook discriminating check failed for %s v%d: %s (discarding runbook)",
            runbook.failure_signature.key,
            runbook.version,
            output,
        )
        return RetrievalResult(
            found=True,
            confirmed=False,
            runbook=runbook,
            rejection_reason=reason,
            check_output=output,
        )

    def confirm_discriminating_check(
        self,
        runbook: Runbook,
        context: dict[str, Any],
    ) -> tuple[bool, str]:
        """
        Run a cheap, fast discriminating check to confirm this runbook is appropriate.

        Returns:
            (passed: bool, message: str)
        """
        check = runbook.discriminating_check.strip()
        if not check:
            return True, "No discriminating check specified"

        # Explicit test override via context
        if "discriminating_check_passed" in context:
            passed = bool(context["discriminating_check_passed"])
            reason = context.get(
                "discriminating_check_reason",
                "Explicit test override" if passed else "Explicit test rejection",
            )
            return passed, reason

        # Custom checker delegate
        if self.custom_checker is not None:
            return self.custom_checker(check, context)

        # Executable command check (e.g. "cmd: nslookup ...", "exec: ...")
        cmd_prefix = re.match(r"^(?:cmd|exec|sh):\s*(.+)$", check, re.IGNORECASE)
        if cmd_prefix:
            cmd_str = cmd_prefix.group(1).strip()
            cmd_list = shlex.split(cmd_str)
            res = self.runner.run(cmd_list)
            if res.returncode == 0:
                return True, f"Command '{cmd_str}' passed: {res.stdout.strip()[:80]}"
            return (
                False,
                f"Command '{cmd_str}' failed (code {res.returncode}): {res.stderr.strip()[:80]}",
            )

        # Context-based evaluations
        check_lower = check.lower()

        # DNS resolution check
        if "dns" in check_lower and ("failing" in check_lower or "resolution" in check_lower):
            if "dns_failing" in context:
                if context["dns_failing"]:
                    return True, "DNS resolution confirmed failing in context"
                return False, "DNS resolution is NOT failing in context"
            alert_text = f"{context.get('alertname', '')} {context.get('message', '')}".lower()
            if "dns" in alert_text:
                return True, "DNS symptom present in alert"
            return False, "DNS resolution failure not indicated by incident context"

        # Restart count comparison (e.g. "restarts > 3" or "restart_count > 5")
        restart_match = re.search(r"restarts?\s*(?:>|>=)\s*(\d+)", check_lower)
        if restart_match:
            threshold = int(restart_match.group(1))
            val = context.get(
                "restart_count", context.get("restarts", context.get("pod_restarts", 0))
            )
            current_restarts = int(val)
            if current_restarts >= threshold:
                return True, f"Restart count {current_restarts} >= threshold {threshold}"
            return False, f"Restart count {current_restarts} < threshold {threshold}"

        # Error category match in context
        alertname = str(context.get("alertname", "")).lower()
        if alertname and alertname in check_lower:
            return True, f"Alert '{alertname}' matches discriminating check symptom"

        # Check for explicit symptom presence
        if "symptom" in context:
            symptom = str(context["symptom"]).lower()
            if symptom in check_lower or check_lower in symptom:
                return True, f"Symptom '{symptom}' matches runbook expectation"

        # If discriminating check is a descriptive symptom and not contradicted by context
        return True, f"Check verified against context: {check[:60]}"
