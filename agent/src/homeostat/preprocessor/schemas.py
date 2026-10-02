"""
Pre-processor schemas — the data contracts between raw logs and the agent.

ErrorSignature is the only thing the LLM ever sees from the log pipeline.
It is never raw log text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawLogLine:
    """A single raw log line from a container or system component."""
    source: str          # "pod/nginx-abc123" or "node/ip-10-0-1-5"
    namespace: str
    message: str
    timestamp: datetime
    stream: str = "stderr"  # "stdout" | "stderr"


@dataclass
class ErrorSignature:
    """
    A clustered, sanitized error signature — the only thing the LLM sees.

    One signature represents a class of error, not a specific instance.
    Variable parts (timestamps, IPs, UUIDs, pod hashes) are replaced with
    placeholders so the same error always produces the same signature.

    Example:
        source:         "deployment/nginx"
        category:       "CrashLoopBackOff"
        pattern_hash:   "a3f9b2c1"
        sample_message: "Error: cannot connect to <IP>:<PORT> — connection refused"
        count:          47
    """
    source: str           # Normalized: "deployment/nginx", "node/*"
    category: str         # Top-level category: "CrashLoopBackOff", "OOMKilled", etc.
    pattern_hash: str     # SHA-256 short hash of the normalized template
    sample_message: str   # One representative message (sanitized, variables replaced)
    count: int            # Number of raw lines in this cluster
    first_seen: datetime
    last_seen: datetime
    namespace: str = ""

    @property
    def key(self) -> str:
        """Unique key for deduplication and runbook lookup."""
        return f"{self.source}:{self.category}:{self.pattern_hash}"

    def to_prompt_text(self) -> str:
        """
        Compact, LLM-readable representation.
        This is exactly what gets injected into the prompt — nothing more.
        """
        return (
            f"[{self.category}] {self.source} "
            f"(ns={self.namespace}, count={self.count}, "
            f"window={self.first_seen.strftime('%H:%M:%S')}–{self.last_seen.strftime('%H:%M:%S')})\n"
            f"  Sample: {self.sample_message}"
        )


@dataclass
class PreprocessorResult:
    """
    The output of one processing window.

    Contains the clustered signatures and any security flags raised during
    sanitization. The agent receives this, not raw logs.
    """
    window_start: datetime
    window_end: datetime
    signatures: list[ErrorSignature] = field(default_factory=list)

    # Security: if any injection attempt was detected, flag it
    # The agent will be told there was a sanitization event but NOT shown the content
    injection_attempts_detected: int = 0
    lines_processed: int = 0
    lines_dropped: int = 0

    @property
    def is_clean(self) -> bool:
        """True if no injection attempts were detected in this window."""
        return self.injection_attempts_detected == 0

    def to_prompt_context(self, max_signatures: int = 10) -> str:
        """
        Format the result as a compact context block for the LLM prompt.
        Hard-capped at max_signatures to control token usage.
        """
        if not self.signatures:
            return "No error signatures detected in this window."

        sigs = self.signatures[:max_signatures]
        lines = [
            f"Error signatures ({len(sigs)} unique, {self.lines_processed} raw lines processed):"
        ]
        if not self.is_clean:
            lines.append(
                f"[SECURITY] {self.injection_attempts_detected} sanitization event(s) detected "
                f"— {self.lines_dropped} lines dropped."
            )
        for i, sig in enumerate(sigs, 1):
            lines.append(f"\n{i}. {sig.to_prompt_text()}")

        return "\n".join(lines)
