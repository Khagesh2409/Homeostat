"""
Subprocess execution runner with logging, timeouts, and injection support.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

from homeostat.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Structured result of executing an external command."""

    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout if self.success else self.stderr


class RunnerFunc(Protocol):
    """Protocol for command runner callables."""

    def __call__(
        self,
        cmd: list[str],
        *,
        timeout_seconds: int = 30,
        input: str | None = None,
    ) -> CommandResult: ...


def default_subprocess_runner(
    cmd: list[str],
    *,
    timeout_seconds: int = 30,
    input: str | None = None,
) -> CommandResult:
    """Execute a command via subprocess.run."""
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            input=input,
        )
        elapsed = time.monotonic() - start
        return CommandResult(
            cmd=cmd,
            returncode=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            elapsed_seconds=elapsed,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        logger.error("Command timed out after %ds: %s", timeout_seconds, " ".join(cmd))
        return CommandResult(
            cmd=cmd,
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout_seconds} seconds",
            elapsed_seconds=elapsed,
        )
    except FileNotFoundError as exc:
        elapsed = time.monotonic() - start
        logger.error("Binary not found: %s", exc)
        return CommandResult(
            cmd=cmd,
            returncode=-1,
            stdout="",
            stderr=f"Executable not found: {exc}",
            elapsed_seconds=elapsed,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.exception("Unexpected error executing %s", cmd)
        return CommandResult(
            cmd=cmd,
            returncode=-1,
            stdout="",
            stderr=f"Command execution error: {exc}",
            elapsed_seconds=elapsed,
        )


class CommandRunner:
    """Manages command execution and test mocking."""

    def __init__(self, runner: RunnerFunc = default_subprocess_runner) -> None:
        self._runner = runner

    def set_runner(self, runner: RunnerFunc) -> None:
        """Inject mock runner for testing."""
        self._runner = runner

    def reset_runner(self) -> None:
        """Reset back to real subprocess execution."""
        self._runner = default_subprocess_runner

    def run(
        self,
        cmd: list[str],
        *,
        timeout_seconds: int | None = None,
        input: str | None = None,
    ) -> CommandResult:
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.tool_timeout_seconds
        )
        logger.info("Executing command: %s (timeout=%ds)", " ".join(cmd), timeout)
        result = self._runner(cmd, timeout_seconds=timeout, input=input)
        if result.success:
            logger.debug("Command succeeded: %s [%.2fs]", " ".join(cmd), result.elapsed_seconds)
        else:
            logger.warning(
                "Command failed (%d): %s | %s",
                result.returncode,
                " ".join(cmd),
                result.stderr or result.stdout,
            )
        return result


# Singleton command runner used by CLI-based tools
default_runner = CommandRunner()
