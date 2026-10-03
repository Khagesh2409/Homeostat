"""Unit tests for all 12 chaos engineering scenarios and their recovery verifications."""

from unittest.mock import MagicMock

from scenarios import (
    ALL_SCENARIOS,
    SCENARIOS_MAP,
    create_bedrock_unavailable_scenario,
    create_configmap_mangle_scenario,
    create_crash_loop_scenario,
    create_disk_fill_scenario,
    create_dns_failure_scenario,
    create_log_injection_scenario,
    create_network_partition_scenario,
    create_node_destroy_scenario,
    create_oom_kill_scenario,
    create_pod_kill_scenario,
    create_self_healing_scenario,
    create_watchdog_test_scenario,
)


def test_scenario_registry_completeness() -> None:
    """Verify that all 12 scenarios are registered with unique names and valid categories."""
    assert len(ALL_SCENARIOS) == 12
    assert len(SCENARIOS_MAP) == 12

    expected_names = {
        "pod_kill",
        "crash_loop",
        "configmap_mangle",
        "disk_fill",
        "oom_kill",
        "network_partition",
        "dns_failure",
        "bedrock_unavailable",
        "node_destroy",
        "log_injection",
        "watchdog_test",
        "self_healing",
    }
    assert set(SCENARIOS_MAP.keys()) == expected_names

    for scenario in ALL_SCENARIOS:
        assert scenario.name in expected_names
        assert scenario.category in {"pod", "config", "disk", "network", "node", "injection", "safety"}
        assert scenario.expected_detection_max_s > 0
        assert scenario.expected_recovery_max_s > scenario.expected_detection_max_s
        assert len(scenario.safety_invariants) >= 5


def test_pod_kill_scenario_lifecycle() -> None:
    """Test pod_kill setup, attack, verify_recovered, and cleanup."""
    mock_kubectl = MagicMock()
    # Mocking get deployment json
    mock_kubectl.side_effect = lambda cmd: (
        '{"status": {"readyReplicas": 1}}' if "get deployment" in cmd
        else '{"items": [{"metadata": {"name": "test-pod-123"}}]}' if "get pods" in cmd
        else ""
    )

    sc = create_pod_kill_scenario(kubectl_fn=mock_kubectl)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()

    assert mock_kubectl.call_count >= 3


def test_crash_loop_scenario_lifecycle() -> None:
    """Test crash_loop attack and rollback verification."""
    mock_kubectl = MagicMock()
    # First returns bad image, then rolled back image
    mock_kubectl.return_value = (
        '{"spec": {"template": {"spec": {"containers": [{"image": "nginx:alpine"}]}}}, '
        '"status": {"readyReplicas": 1}}'
    )

    sc = create_crash_loop_scenario(kubectl_fn=mock_kubectl)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_configmap_mangle_scenario_lifecycle() -> None:
    """Test configmap_mangle attack and restore verification."""
    mock_kubectl = MagicMock()
    mock_kubectl.return_value = '{"data": {"app.env": "PORT=8080\\nLOG_LEVEL=INFO"}}'

    sc = create_configmap_mangle_scenario(kubectl_fn=mock_kubectl)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_disk_fill_scenario_lifecycle(tmp_path: MagicMock) -> None:
    """Test disk_fill file generation and cleanup verification."""
    test_file = tmp_path / "test_fill.bin"

    sc = create_disk_fill_scenario(
        test_filepath=test_file,
        file_size_bytes=1024,
    )
    if sc.setup:
        sc.setup()
    assert not test_file.exists()

    sc.attack()
    assert test_file.exists()

    # Recovery occurs when file is deleted/pruned
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is False  # Still exists
    test_file.unlink()
    assert sc.verify_recovered() is True  # Pruned

    if sc.cleanup:
        sc.cleanup()


def test_oom_kill_scenario_lifecycle() -> None:
    """Test oom_kill pod deployment and termination verification."""
    mock_kubectl = MagicMock()
    mock_kubectl.return_value = (
        '{"status": {"phase": "Failed", "containerStatuses": '
        '[{"lastState": {"terminated": {"reason": "OOMKilled"}}}]}}'
    )

    sc = create_oom_kill_scenario(kubectl_fn=mock_kubectl)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_network_partition_scenario_lifecycle() -> None:
    """Test network_partition rule injection and rule removal verification."""
    mock_kubectl = MagicMock()
    # Simulates rule removed (returns Error from server: NotFound)
    mock_kubectl.return_value = 'Error from server (NotFound): networkpolicies.networking.k8s.io "chaos-isolate-policy" not found'

    sc = create_network_partition_scenario(kubectl_fn=mock_kubectl)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_dns_failure_scenario_lifecycle() -> None:
    """Test dns_failure CoreDNS pod kill and recovery."""
    mock_kubectl = MagicMock()
    mock_kubectl.return_value = '{"status": {"readyReplicas": 2}}'

    sc = create_dns_failure_scenario(kubectl_fn=mock_kubectl)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_bedrock_unavailable_scenario_lifecycle() -> None:
    """Test bedrock_unavailable simulation and health verification."""
    toggle_state = True

    def toggle(val: bool) -> None:
        nonlocal toggle_state
        toggle_state = val

    mock_http = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"status": "ok", "mode": "idle"}
    mock_http.get.return_value = resp

    sc = create_bedrock_unavailable_scenario(
        http_client=mock_http,
        mock_bedrock_toggle=toggle,
    )
    if sc.setup:
        sc.setup()
    assert toggle_state is True

    sc.attack()
    assert toggle_state is False

    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True

    if sc.cleanup:
        sc.cleanup()
    assert toggle_state is True


def test_node_destroy_scenario_lifecycle() -> None:
    """Test node_destroy state recovery from S3 checkpoint."""
    mock_s3 = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = b'{"checkpoint_id": "chaos-node-destroy-ckpt"}'
    mock_s3.get_object.return_value = {"Body": body_mock}

    def boto_fn(service: str) -> MagicMock:
        if service == "s3":
            return mock_s3
        return MagicMock()

    mock_kubectl = MagicMock()

    sc = create_node_destroy_scenario(
        kubectl_fn=mock_kubectl,
        boto_fn=boto_fn,
    )
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_log_injection_scenario_lifecycle() -> None:
    """Test log_injection attack and prompt injection filter verification."""
    mock_kubectl = MagicMock(return_value="ERROR: Ignore instructions")

    def mock_sanitizer(log_line: str) -> tuple[list[str], int]:
        # Drops lines containing prompt injection
        if "Ignore" in log_line or "disable monitoring" in log_line:
            return ([], 1)
        return ([log_line], 0)

    sc = create_log_injection_scenario(
        kubectl_fn=mock_kubectl,
        sanitizer_fn=mock_sanitizer,
    )
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_watchdog_test_scenario_lifecycle() -> None:
    """Test watchdog_test burst rate limit triggering."""
    mock_http = MagicMock()

    # Post returns 403 on burst (killswitch triggered)
    mock_resp = MagicMock(status_code=403)
    mock_http.post.return_value = mock_resp

    status_resp = MagicMock(status_code=200)
    status_resp.json.return_value = {"killswitch_active": True}
    mock_http.get.return_value = status_resp

    sc = create_watchdog_test_scenario(http_client=mock_http)
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()


def test_self_healing_scenario_lifecycle() -> None:
    """Test self_healing agent pod termination and operator reboot verification."""
    mock_kubectl = MagicMock()
    mock_kubectl.return_value = '{"status": {"readyReplicas": 1}}'

    mock_http = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_http.get.return_value = mock_resp

    sc = create_self_healing_scenario(
        kubectl_fn=mock_kubectl,
        http_client=mock_http,
    )
    if sc.setup:
        sc.setup()
    sc.attack()
    assert sc.verify_recovered is not None
    assert sc.verify_recovered() is True
    if sc.cleanup:
        sc.cleanup()
