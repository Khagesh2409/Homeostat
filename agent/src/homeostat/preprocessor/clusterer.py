"""
Log clusterer — groups raw log lines into a small set of ErrorSignatures.

Design:
  1. Normalize each line: replace all variable parts with placeholders
     (timestamps, IPs, UUIDs, pod hash suffixes, hex addresses, numbers)
  2. Use the normalized template as the cluster key — same error class → same key
  3. Group lines by their key, keep one representative sample per group
  4. Categorize each cluster using keyword matching
  5. Emit at most MAX_SIGNATURES_PER_WINDOW clusters

This is entirely deterministic — no ML, no embeddings, no external calls.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from datetime import datetime

from homeostat.preprocessor.schemas import ErrorSignature, RawLogLine

logger = logging.getLogger(__name__)

MAX_SIGNATURES_PER_WINDOW = 10

# ── Normalization regexes ─────────────────────────────────────────────────────
# Applied in order — each replaces a variable token with a placeholder.
# The resulting "template" is what we cluster on.

_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    # ISO timestamps: 2024-01-15T10:23:45.123Z
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # UUIDs — MUST come before EPOCH/HEX so the dashed format is matched whole
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE), "<UUID>"),
    # Unix timestamps (10-digit epoch or with milliseconds)
    (re.compile(r"\b\d{10,13}\b"), "<EPOCH>"),
    # IPv6 addresses
    (re.compile(r"(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}"), "<IPV6>"),
    # IPv4 addresses with optional port
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b"), "<IP>"),
    # Kubernetes pod hash suffixes (e.g. nginx-7d9f8b-xkqp2 → nginx-<POD>)
    (re.compile(r"\b([a-z][a-z0-9-]+)-[a-z0-9]{5,10}-[a-z0-9]{5}\b"), r"\1-<POD>"),
    # 0x-prefixed hex addresses (e.g. 0x00007f8a3c2d1000) — before bare HEX
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    # Bare long hex strings (SHA hashes, etc.) — word-boundary
    (re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE), "<HEX>"),
    # Port numbers (standalone, e.g. ":8080" or "port 8080")
    (re.compile(r"(?:port\s+|:)\d{2,5}\b"), "<PORT>"),
    # Pure numbers ≥ 4 digits (resource IDs, PIDs, etc.) — keep small numbers
    (re.compile(r"\b\d{4,}\b"), "<NUM>"),
    # File paths with variable components
    (re.compile(r"/(?:var|tmp|proc|sys|home|data)/\S+"), "<PATH>"),
    # Container image tags (e.g. nginx:1.25.3-alpine)
    (re.compile(r":[0-9]+\.[0-9]+\.[0-9][-\w]*"), ":<VER>"),
]

# ── Category detection keywords ───────────────────────────────────────────────
# Maps keyword → category name. First match wins.

_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    # Specific patterns MUST come before generic ones ("Error" is last resort)
    ("CrashLoopBackOff",  "CrashLoopBackOff"),
    ("Back-off",           "CrashLoopBackOff"),  # K8s uses "Back-off" in event messages
    ("BackOff",            "CrashLoopBackOff"),
    ("OOMKilled",          "OOMKilled"),
    ("OOM",                "OOMKilled"),
    ("out of memory",      "OOMKilled"),
    ("killed",             "OOMKilled"),
    ("FailedScheduling",   "FailedScheduling"),
    ("Evicted",            "Evicted"),
    ("ImagePullBackOff",   "ImagePullBackOff"),
    ("ErrImagePull",       "ImagePullBackOff"),
    ("Unhealthy",          "Unhealthy"),
    ("readiness probe",    "Unhealthy"),
    ("liveness probe",     "Unhealthy"),
    ("disk pressure",      "DiskPressure"),
    ("DiskPressure",       "DiskPressure"),
    ("memory pressure",    "MemoryPressure"),
    ("MemoryPressure",     "MemoryPressure"),
    ("timeout",            "Timeout"),
    ("connection refused", "ConnectionRefused"),
    ("connection reset",   "ConnectionReset"),
    ("TLS",                "TLSError"),
    ("certificate",        "TLSError"),
    ("permission denied",  "PermissionDenied"),
    ("panic",              "Panic"),
    ("fatal",              "Fatal"),
    # Generic — must be last
    ("Exception",          "Error"),
    ("exception",          "Error"),
    ("Error",              "Error"),
    ("error",              "Error"),
]


def _normalize(line: str) -> str:
    """Replace variable tokens with placeholders. Returns the template string."""
    result = line
    for pattern, replacement in _NORMALIZERS:
        result = pattern.sub(replacement, result)
    # Collapse repeated whitespace
    result = re.sub(r"\s+", " ", result).strip()
    return result


def _categorize(line: str) -> str:
    """Extract a category from a log line using keyword matching."""
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in line:
            return category
    return "Unknown"


def _source_normalize(source: str) -> str:
    """
    Normalize a pod/node source name.
    'pod/nginx-7d9f8b-xkqp2' → 'deployment/nginx'
    'node/ip-10-0-1-5' → 'node/*'
    """
    if source.startswith("pod/"):
        # Strip pod hash: pod/nginx-abc123-xyz12 → deployment/nginx
        pod_name = source.removeprefix("pod/")
        # Remove trailing hash pairs (e.g. -7d9f8b-xkqp2)
        normalized = re.sub(r"-[a-z0-9]{5,10}-[a-z0-9]{5}$", "", pod_name)
        normalized = re.sub(r"-[a-z0-9]{5}$", "", normalized)
        return f"deployment/{normalized}"
    if source.startswith("node/"):
        return "node/*"
    return source


class LogClusterer:
    """
    Clusters a list of RawLogLines into at most MAX_SIGNATURES_PER_WINDOW
    ErrorSignatures per window.

    Usage:
        clusterer = LogClusterer()
        signatures = clusterer.cluster(raw_lines, window_start, window_end)
    """

    def cluster(
        self,
        lines: list[RawLogLine],
        window_start: datetime,
        window_end: datetime,
    ) -> list[ErrorSignature]:
        """
        Process a batch of raw log lines and return clustered signatures.
        """
        if not lines:
            return []

        # Group lines by (normalized_source, normalized_template)
        # cluster_key → list of matching lines
        clusters: dict[str, list[RawLogLine]] = defaultdict(list)

        for line in lines:
            template = _normalize(line.message)
            norm_source = _source_normalize(line.source)
            key = f"{norm_source}||{template}"
            clusters[key].append(line)

        # Convert each cluster to an ErrorSignature
        signatures: list[ErrorSignature] = []
        for key, cluster_lines in clusters.items():
            norm_source, template = key.split("||", 1)

            # Representative sample from the first line
            sample = cluster_lines[0].message
            # Truncate to keep prompt tokens bounded
            if len(sample) > 300:
                sample = sample[:297] + "..."

            category = _categorize(sample)

            # Short hash of the template for deduplication
            pattern_hash = hashlib.sha256(template.encode()).hexdigest()[:8]

            timestamps = [l.timestamp for l in cluster_lines]

            sig = ErrorSignature(
                source=norm_source,
                category=category,
                pattern_hash=pattern_hash,
                sample_message=_normalize(sample),
                count=len(cluster_lines),
                first_seen=min(timestamps),
                last_seen=max(timestamps),
                namespace=cluster_lines[0].namespace,
            )
            signatures.append(sig)
            logger.debug("Cluster: %s [%d lines]", sig.key, sig.count)

        # Sort by count descending (most frequent errors first)
        signatures.sort(key=lambda s: s.count, reverse=True)

        # Hard cap
        if len(signatures) > MAX_SIGNATURES_PER_WINDOW:
            dropped = len(signatures) - MAX_SIGNATURES_PER_WINDOW
            logger.info(
                "Clusterer: capping at %d signatures (%d clusters dropped)",
                MAX_SIGNATURES_PER_WINDOW,
                dropped,
            )
            signatures = signatures[:MAX_SIGNATURES_PER_WINDOW]

        return signatures
