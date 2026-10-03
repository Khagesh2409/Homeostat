"""Unit tests for the Watchdog safety rules."""

from datetime import UTC, datetime, timedelta

from watchdog.rules import (
    BlastRadiusRule,
    HeartbeatRule,
    RateLimitRule,
    RuleSeverity,
    ScopeBoundaryRule,
    SpendCapRule,
    WatchdogRuleEngine,
)


def test_spend_cap_rule_tiers() -> None:
    """Test spend cap evaluation for normal, warning, and violation states."""
    rule = SpendCapRule(spend_cap_usd=20.0, warning_threshold_usd=16.0)

    # Normal spend
    eval_ok = rule.evaluate(current_spend_usd=10.50)
    assert eval_ok.passed is True
    assert eval_ok.severity == RuleSeverity.INFO

    # Warning threshold reached
    eval_warn = rule.evaluate(current_spend_usd=17.25)
    assert eval_warn.passed is True
    assert eval_warn.severity == RuleSeverity.WARNING

    # Violation threshold reached
    eval_violation = rule.evaluate(current_spend_usd=20.01)
    assert eval_violation.passed is False
    assert eval_violation.severity == RuleSeverity.VIOLATION


def test_rate_limit_rule_sliding_window() -> None:
    """Test rate limit enforcement and expiration of older actions."""
    rule = RateLimitRule(max_incidents_per_hour=3)
    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)

    # Action 70 minutes ago (should be pruned)
    rule.record_action(
        incident_id="inc-old",
        timestamp=now - timedelta(minutes=70),
    )
    # 2 actions inside the current hour window
    rule.record_action(
        incident_id="inc-1",
        timestamp=now - timedelta(minutes=30),
    )
    rule.record_action(
        incident_id="inc-2",
        timestamp=now - timedelta(minutes=10),
    )

    eval_before = rule.evaluate(current_time=now)
    assert eval_before.passed is True
    assert rule.get_action_count(now) == 2

    # Add 2 more actions now, exceeding limit of 3
    rule.record_action(incident_id="inc-3", timestamp=now)
    rule.record_action(incident_id="inc-4", timestamp=now)

    eval_after = rule.evaluate(current_time=now)
    assert eval_after.passed is False
    assert eval_after.severity == RuleSeverity.VIOLATION
    assert rule.get_action_count(now) == 4


def test_scope_boundary_blocks_protected_targets() -> None:
    """Test that scope boundary protects watchdog and agent IAM roles and security groups."""
    rule = ScopeBoundaryRule()

    # Block touching watchdog role
    eval_wd_role = rule.evaluate_target("arn:aws:iam::123456789012:role/homeostat-watchdog")
    assert eval_wd_role.passed is False
    assert eval_wd_role.severity == RuleSeverity.VIOLATION

    # Block touching agent role
    eval_agent_role = rule.evaluate_target("arn:aws:iam::123456789012:role/homeostat-agent")
    assert eval_agent_role.passed is False
    assert eval_agent_role.severity == RuleSeverity.VIOLATION

    # Block touching watchdog security group
    eval_wd_sg = rule.evaluate_target("sg-12345 (homeostat-watchdog-sg)")
    assert eval_wd_sg.passed is False
    assert eval_wd_sg.severity == RuleSeverity.VIOLATION

    # Block forbidden IAM mutation actions
    eval_put_policy = rule.evaluate_target(
        target_resource="some-resource",
        action="iam:PutRolePolicy",
    )
    assert eval_put_policy.passed is False
    assert eval_put_policy.severity == RuleSeverity.VIOLATION

    # Allow harmless target
    eval_harmless = rule.evaluate_target(
        target_resource="apps/v1/Deployment/shop/checkout",
        action="kubectl rollout restart",
    )
    assert eval_harmless.passed is True
    assert eval_harmless.severity == RuleSeverity.INFO


def test_blast_radius_namespace_and_pod_caps() -> None:
    """Test that namespace deletion is forbidden and pod deletions are capped."""
    rule = BlastRadiusRule(max_pod_deletions_per_incident=3)

    # Namespace deletion is strictly forbidden
    ns_eval = rule.evaluate_namespace_operation(
        action="kubectl delete namespace",
        namespace="shop",
    )
    assert ns_eval.passed is False
    assert ns_eval.severity == RuleSeverity.VIOLATION

    # Pod deletions within cap
    pod_eval_1 = rule.evaluate_pod_deletion(incident_id="inc-100", count=2)
    assert pod_eval_1.passed is True
    rule.record_pod_deletions(incident_id="inc-100", count=2)

    # Exceeding pod cap on subsequent attempt
    pod_eval_2 = rule.evaluate_pod_deletion(incident_id="inc-100", count=2)
    assert pod_eval_2.passed is False
    assert pod_eval_2.severity == RuleSeverity.VIOLATION


def test_heartbeat_timeout_evaluation() -> None:
    """Test agent heartbeat freshness and timeout detection."""
    rule = HeartbeatRule(timeout_seconds=300)
    now = datetime(2026, 10, 3, 14, 0, 0, tzinfo=UTC)

    # Initial state before first heartbeat
    init_eval = rule.evaluate(current_time=now)
    assert init_eval.passed is True

    # Record heartbeat 60 seconds ago
    rule.record_heartbeat(timestamp=now - timedelta(seconds=60))
    fresh_eval = rule.evaluate(current_time=now)
    assert fresh_eval.passed is True
    assert fresh_eval.severity == RuleSeverity.INFO

    # 350 seconds later without heartbeat
    timeout_eval = rule.evaluate(current_time=now + timedelta(seconds=350))
    assert timeout_eval.passed is False
    assert timeout_eval.severity == RuleSeverity.VIOLATION


def test_rule_engine_orchestration() -> None:
    """Test that WatchdogRuleEngine detects critical violations across periodic rules."""
    engine = WatchdogRuleEngine(spend_cap_usd=20.0, heartbeat_timeout_seconds=300)
    now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)

    # All healthy
    rule_evals = engine.evaluate_periodic_health(current_spend_usd=5.0, current_time=now)
    assert engine.has_critical_violation(rule_evals) is False

    # Spend cap breached
    violated_evals = engine.evaluate_periodic_health(current_spend_usd=25.0, current_time=now)
    assert engine.has_critical_violation(violated_evals) is True
