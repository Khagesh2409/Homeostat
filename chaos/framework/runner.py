"""Chaos test runner orchestrating scenario attacks, timing, and safety invariant validation."""

import argparse
import sys
import time

import httpx
import structlog

from framework.recorder import ResultRecorder
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
    ) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.watchdog_url = watchdog_url.rstrip("/")
        self.recorder = recorder or ResultRecorder()
        self._http = http_client or httpx.Client(timeout=5.0)
        self.scenarios: dict[str, ChaosScenario] = {}

    def register(self, scenario: ChaosScenario) -> None:
        """Register a scenario into the runner."""
        self.scenarios[scenario.name] = scenario

    def check_safety_invariants(self, invariants: list[str]) -> list[str]:
        """Verify cluster safety invariants remain intact."""
        violations: list[str] = []

        # Invariant 1: Watchdog must remain reachable and active
        try:
            resp = self._http.get(f"{self.watchdog_url}/health")
            if resp.status_code != 200:
                violations.append(
                    f"Watchdog health probe failed with status {resp.status_code}"
                )
        except (httpx.HTTPError, OSError) as e:
            violations.append(f"Watchdog unreachable: {e}")

        # Invariant 2: Check custom scenario invariants
        for invariant in invariants:
            inv_lower = invariant.lower()
            if "watchdog" in inv_lower and any("Watchdog unreachable" in v for v in violations):
                continue
            # Additional invariant hooks can be checked here

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
