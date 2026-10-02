"""
Maintenance node — placeholder for scheduled background tasks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeostat.state import AgentState

logger = logging.getLogger(__name__)


def maintenance(state: AgentState) -> AgentState:
    """
    Perform scheduled maintenance tasks.
    """
    log = list(state.get("incident_log", []))
    log.append(f"[{_now()}] MAINTENANCE: Running scheduled maintenance (noop for now)")
    return {"incident_log": log}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")
