"""Homeostat Watchdog — External Safety Leash."""

from watchdog.config import WatchdogSettings, get_settings
from watchdog.killswitch import KillSwitch, KillSwitchRecord
from watchdog.rules import (
    BlastRadiusRule,
    HeartbeatRule,
    RateLimitRule,
    RuleEvaluation,
    RuleSeverity,
    ScopeBoundaryRule,
    SpendCapRule,
    WatchdogRuleEngine,
)
from watchdog.service import WatchdogService, app

__all__ = [
    "BlastRadiusRule",
    "HeartbeatRule",
    "KillSwitch",
    "KillSwitchRecord",
    "RateLimitRule",
    "RuleEvaluation",
    "RuleSeverity",
    "ScopeBoundaryRule",
    "SpendCapRule",
    "WatchdogRuleEngine",
    "WatchdogService",
    "WatchdogSettings",
    "app",
    "get_settings",
]
