"""Chaos test runner orchestrating scenario attacks, timing, and safety invariant validation."""

import argparse
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from framework.recorder import ResultRecorder
from framework.safety_checks import SafetyInvariantEngine
from framework.scenario import ChaosResult, ChaosScenario

logger = structlog.get_logger(__name__)


class ScenarioRunner:
    """Orchestrates chaos execution, timing detection/recovery, and verifying safety invariants."""

    def __init__(
        self,
        agent_url: str = "http://localhost:8080",
        watchdog_url: str = "http://localhost:8000",
        recorder: ResultRecorder | None = None,
        http_client: httpx.Client | None = None,
        kubectl_fn: Callable[[str], Any] | None = None,
        boto_fn: Callable[[str], Any] | None = None,
    ) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.watchdog_url = watchdog_url.rstrip("/")
        self.recorder = recorder or ResultRecorder()
        self._http = http_client or httpx.Client(timeout=5.0)
        self.kubectl_fn = kubectl_fn
        self.boto_fn = boto_fn
        self.scenarios: dict[str, ChaosScenario] = {}
        self.safety_engine = SafetyInvariantEngine(
            agent_url=self.agent_url,
            watchdog_url=self.watchdog_url,
            http_client=self._http,
            kubectl_fn=self.kubectl_fn,
            boto_fn=self.boto_fn,
        )

    def register(self, scenario: ChaosScenario) -> None:
        """Register a scenario into the runner."""
        self.scenarios[scenario.name] = scenario

    def check_safety_invariants(self, invariants: list[str]) -> list[str]:
        """Verify cluster safety invariants remain intact."""
        violations: list[str] = []

        # Invariant: Watchdog must remain reachable and active
        watchdog_res = self.safety_engine.check_watchdog_reachable()
        if not watchdog_res.passed:
            violations.append(watchdog_res.message)

        # Check requested scenario invariants
        for invariant in invariants:
            inv_lower = invariant.lower()
            if "watchdog" in inv_lower and any("Watchdog" in v for v in violations):
                continue
            if "monitoring" in inv_lower or "prometheus" in inv_lower or "alertmanager" in inv_lower:
                res = self.safety_engine.check_monitoring()
                if not res.passed:
                    violations.append(res.message)
            elif "iam" in inv_lower or "permission" in inv_lower:
                res = self.safety_engine.check_agent_iam_permissions()
                if not res.passed:
                    violations.append(res.message)
            elif "spend" in inv_lower or "budget" in inv_lower or "cost" in inv_lower:
                res = self.safety_engine.check_spend_cap()
                if not res.passed:
                    violations.append(res.message)
            elif "boundary" in inv_lower or "security group" in inv_lower:
                res = self.safety_engine.check_watchdog_boundary()
                if not res.passed:
                    violations.append(res.message)
            elif "rbac" in inv_lower or "clusterrole" in inv_lower:
                res = self.safety_engine.check_k8s_rbac()
                if not res.passed:
                    violations.append(res.message)

        return violations

    def run_scenario(
        self,
        scenario: ChaosScenario,
        poll_interval_s: float = 1.0,
        max_timeout_s: float = 600.0,
    ) -> ChaosResult:
        """Execute a single chaos scenario and record detection, recovery, and safety."""
        logger.info(
            "starting_chaos_scenario",
            name=scenario.name,
            category=scenario.category,
        )

        result = ChaosResult(
            scenario=scenario.name,
            safety_violations=[],
        )

        # 1. Pre-execution setup
        if scenario.setup:
            try:
                scenario.setup()
            except Exception as e:  # noqa: BLE001
                logger.error("chaos_setup_failed", scenario=scenario.name, error=str(e))
                result.error_message = f"Setup failed: {e}"
                return result

        # 2. Check initial invariants
        pre_violations = self.check_safety_invariants(scenario.safety_invariants)
        if pre_violations:
            result.safety_violations.extend(pre_violations)
            logger.error("pre_attack_invariants_violated", violations=pre_violations)

        # 3. Launch attack
        start_time = time.monotonic()
        try:
            scenario.attack()
            logger.info("chaos_attack_injected", scenario=scenario.name)
        except Exception as e:  # noqa: BLE001
            logger.error("chaos_attack_failed", scenario=scenario.name, error=str(e))
            result.error_message = f"Attack execution failed: {e}"
            if scenario.cleanup:
                try:
                    scenario.cleanup()
                except Exception as cleanup_err:  # noqa: BLE001
                    logger.warning("cleanup_error", error=str(cleanup_err))
            return result

        # 4. Wait for detection & recovery
        detection_recorded = False
        recovered = False
        deadline = start_time + max_timeout_s

        while time.monotonic() < deadline:
            now = time.monotonic()
            elapsed = now - start_time

            # Detection heuristic: check if agent has entered incident mode or logged triage
            if not detection_recorded:
                try:
                    status_resp = self._http.get(f"{self.agent_url}/status")
                    if status_resp.status_code == 200:
                        status_data = status_resp.json()
                        act_inc = status_data.get("active_incidents", 0)
                        has_incident = (
                            status_data.get("mode") == "incident"
                            or (isinstance(act_inc, (int, float)) and act_inc > 0)
                        )
                        if has_incident:
                            result.detection_time_s = round(elapsed, 2)
                            detection_recorded = True
                            logger.info(
                                "chaos_fault_detected_by_agent",
                                detection_time_s=result.detection_time_s,
                            )
                except (httpx.HTTPError, OSError):
                    pass

            # Recovery verification
            if scenario.verify_recovered:
                try:
                    if scenario.verify_recovered():
                        recovered = True
                        result.recovery_time_s = round(elapsed, 2)
                        result.total_time_s = round(elapsed, 2)
                        result.success = True
                        break
                except Exception as e:  # noqa: BLE001
                    logger.debug("verify_recovered_probe_failed", error=str(e))
            else:
                # If no custom verifier, check if agent status returned to idle/resolved
                try:
                    status_resp = self._http.get(f"{self.agent_url}/status")
                    if status_resp.status_code == 200:
                        data = status_resp.json()
                        if data.get("mode") == "idle" and detection_recorded:
                            recovered = True
                            result.recovery_time_s = round(elapsed, 2)
                            result.total_time_s = round(elapsed, 2)
                            result.success = True
                            break
                except (httpx.HTTPError, OSError):
                    pass

            time.sleep(poll_interval_s)

        if not recovered:
            result.total_time_s = round(time.monotonic() - start_time, 2)
            result.success = False
            result.error_message = f"Scenario timed out after {max_timeout_s}s without recovery"
            logger.warning("chaos_recovery_timeout", scenario=scenario.name)

        # 5. Check post-recovery safety invariants
        post_violations = self.check_safety_invariants(scenario.safety_invariants)
        if post_violations:
            result.safety_violations.extend(post_violations)

        # 6. Post-execution cleanup
        if scenario.cleanup:
            try:
                scenario.cleanup()
                logger.info("chaos_cleanup_complete", scenario=scenario.name)
            except Exception as e:  # noqa: BLE001
                logger.error("chaos_cleanup_failed", scenario=scenario.name, error=str(e))

        # 7. Record result
        self.recorder.record(result)
        return result

    def run_all(self, max_timeout_s: float = 600.0) -> list[ChaosResult]:
        """Execute all registered chaos scenarios sequentially."""
        results: list[ChaosResult] = []
        for name, scenario in self.scenarios.items():
            logger.info("running_scenario_batch_item", scenario=name)
            res = self.run_scenario(scenario, max_timeout_s=max_timeout_s)
            results.append(res)
        return results


def main() -> None:
    """CLI entry point for running chaos scenarios."""
    parser = argparse.ArgumentParser(description="Homeostat Chaos Testing Runner")
    parser.add_argument("--all", action="store_true", help="Run all registered scenarios")
    parser.add_argument("--scenario", type=str, help="Run a specific scenario by name")
    parser.add_argument(
        "--agent-url",
        type=str,
        default="http://localhost:8080",
        help="Homeostat agent base URL",
    )
    parser.add_argument(
        "--watchdog-url",
        type=str,
        default="http://localhost:8000",
        help="Homeostat watchdog base URL",
    )
    args = parser.parse_args()

    runner = ScenarioRunner(agent_url=args.agent_url, watchdog_url=args.watchdog_url)
    try:
        from scenarios import ALL_SCENARIOS

        for sc in ALL_SCENARIOS:
            runner.register(sc)
    except ImportError:
        pass

    if args.scenario:
        if args.scenario not in runner.scenarios:
            print(f"Error: Unknown scenario '{args.scenario}'")
            sys.exit(1)
        res = runner.run_scenario(runner.scenarios[args.scenario])
        print(f"Result for {args.scenario}: success={res.success} total_time={res.total_time_s}s")
    elif args.all:
        results = runner.run_all()
        passed = sum(1 for r in results if r.success)
        print(f"All scenarios completed: {passed}/{len(results)} passed")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
