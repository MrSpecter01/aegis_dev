import pytest
from unittest.mock import AsyncMock

from services.phase2_worker.convergence import (
    extract_scalar_from_prometheus_result,
    run_convergence_monitor,
)
from shared.config.schemas import (
    SystemConfig,
    ConvergenceConfig,
    ConvergenceSLOConfig,
)


def test_extract_scalar_from_prometheus_result():
    # Direct scalar
    assert extract_scalar_from_prometheus_result(0.05) == 0.05
    assert extract_scalar_from_prometheus_result("123.45") == 123.45
    assert extract_scalar_from_prometheus_result(None) is None
    assert extract_scalar_from_prometheus_result("invalid") is None

    # Results dictionary format
    results_format = {
        "results": [
            {
                "metric": {"service": "cartservice"},
                "values": [[1710000000, "0.004"]],
            }
        ]
    }
    assert extract_scalar_from_prometheus_result(results_format) == 0.004

    # FastMCP content block text format
    fastmcp_format = {
        "content": [
            {
                "type": "text",
                "text": '{"results": [{"values": [[1710000000, "250.5"]]}]}',
            }
        ]
    }
    assert extract_scalar_from_prometheus_result(fastmcp_format) == 250.5


@pytest.mark.asyncio
async def test_run_convergence_monitor_converged_stateless():
    slept_durations = []

    async def mock_sleep(seconds: float):
        slept_durations.append(seconds)

    # Simulated clock
    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    # Mock client returns healthy metrics
    async def mock_prometheus(query: str):
        if "rate(http_requests_total" in query:
            return 0.002  # 0.2% error rate <= 1.0% threshold
        if "histogram_quantile" in query:
            return 120.0  # 120ms latency <= 500ms threshold
        if "container_cpu_usage" in query:
            return 0.45  # 45% CPU <= 85% threshold
        if "container_memory" in query:
            return 0.60  # 60% memory <= 90% threshold
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=30,
            bake_time_seconds_stateful=60,
            poll_interval_seconds=10,
            window_seconds=30,  # 3 clean samples needed
            slo=ConvergenceSLOConfig(),
        )
    )

    converged, latest_signals = await run_convergence_monitor(
        incident_id="inc-123",
        affected_services=["cartservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
    )

    assert converged is True
    assert latest_signals["cartservice.error_rate"] == 0.002
    assert latest_signals["cartservice.p99_latency_ms"] == 120.0
    assert latest_signals["cartservice.cpu_saturation"] == 0.45
    assert latest_signals["cartservice.memory_saturation"] == 0.60
    assert latest_signals["_meta"]["bake_time_seconds"] == 30
    assert latest_signals["_meta"]["clean_samples"] == 3
    assert latest_signals["_meta"]["target_clean_samples"] == 3
    assert latest_signals["_meta"]["monitored_services"] == ["cartservice"]
    assert 30 in slept_durations  # Stateless bake time honored


@pytest.mark.asyncio
async def test_run_convergence_monitor_bake_time_stateful():
    slept_durations = []

    async def mock_sleep(seconds: float):
        slept_durations.append(seconds)

    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    async def mock_prometheus(query: str):
        return 0.001

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=30,
            bake_time_seconds_stateful=90,
            poll_interval_seconds=10,
            window_seconds=10,
            slo=ConvergenceSLOConfig(),
        )
    )

    converged, _ = await run_convergence_monitor(
        incident_id="inc-456",
        affected_services=["redis-cart"],
        is_stateful=True,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
    )

    assert converged is True
    assert 90 in slept_durations  # Stateful bake time honored


@pytest.mark.asyncio
async def test_run_convergence_monitor_timeout_on_persistent_violation():
    slept_durations = []

    async def mock_sleep(seconds: float):
        slept_durations.append(seconds)

    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    # Persistent 5% error rate (violates 1% threshold)
    async def mock_prometheus(query: str):
        if "http_requests_total" in query:
            return 0.05
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=20,  # Deadline expires quickly
            slo=ConvergenceSLOConfig(error_rate_threshold=0.01),
        )
    )

    converged, latest_signals = await run_convergence_monitor(
        incident_id="inc-789",
        affected_services=["frontend"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
    )

    assert converged is False
    assert latest_signals.get("frontend.error_rate") == 0.05


@pytest.mark.asyncio
async def test_run_convergence_monitor_window_resets_on_intermittent_violation():
    slept_durations = []

    async def mock_sleep(seconds: float):
        slept_durations.append(seconds)

    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    sample_count = 0

    async def mock_prometheus(query: str):
        nonlocal sample_count
        if "http_requests_total" in query:
            sample_count += 1
            if sample_count == 2:
                return 0.08  # 2nd sample spikes to 8% -> triggers reset
            return 0.002
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=30,  # Needs 3 consecutive clean samples
            slo=ConvergenceSLOConfig(error_rate_threshold=0.01),
        )
    )

    # Window duration allows 5 iterations before deadline
    cfg.convergence.window_seconds = 60

    converged, _ = await run_convergence_monitor(
        incident_id="inc-999",
        affected_services=["checkoutservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
        required_clean_samples=3,
    )

    assert converged is True
    # At least 4 iterations happened (1 clean, 1 fail, 3 clean after reset)
    assert sample_count >= 4


def test_extract_scalar_rejects_non_finite_values():
    assert extract_scalar_from_prometheus_result(float("nan")) is None
    assert extract_scalar_from_prometheus_result(float("inf")) is None
    assert extract_scalar_from_prometheus_result(float("-inf")) is None
    assert extract_scalar_from_prometheus_result("NaN") is None
    assert extract_scalar_from_prometheus_result("nan") is None
    assert extract_scalar_from_prometheus_result("inf") is None
    assert extract_scalar_from_prometheus_result("-inf") is None
    assert extract_scalar_from_prometheus_result({"data": {"result": [{"value": [1710000000, "NaN"]}]}}) is None
    assert extract_scalar_from_prometheus_result({"data": {"result": [{"value": [1710000000, "+Inf"]}]}}) is None
    assert extract_scalar_from_prometheus_result({"results": [{"values": [[1710000000, "NaN"]]}]}) is None


@pytest.mark.asyncio
async def test_nan_sample_treated_as_failure():
    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    async def mock_sleep(_):
        pass

    async def mock_prometheus(query: str):
        if "rate(http_requests_total" in query:
            return float("nan")
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=20,
            slo=ConvergenceSLOConfig(),
        )
    )

    converged, latest = await run_convergence_monitor(
        incident_id="inc-nan",
        affected_services=["authservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
    )
    assert converged is False


@pytest.mark.asyncio
async def test_inf_sample_treated_as_failure():
    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    async def mock_sleep(_):
        pass

    async def mock_prometheus(query: str):
        if "container_cpu_usage" in query:
            return float("inf")
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=20,
            slo=ConvergenceSLOConfig(),
        )
    )

    converged, latest = await run_convergence_monitor(
        incident_id="inc-inf",
        affected_services=["paymentservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
    )
    assert converged is False


@pytest.mark.asyncio
async def test_memory_ratio_below_threshold_converges():
    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    async def mock_sleep(_):
        pass

    async def mock_prometheus(query: str):
        if "container_memory_working_set_bytes" in query:
            return 0.75  # 75% memory ratio <= 90% threshold
        if "container_cpu_usage" in query:
            return 0.40
        if "http_request_duration_seconds" in query:
            return 150.0
        if "http_requests_total" in query:
            return 0.001
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=20,
            slo=ConvergenceSLOConfig(memory_saturation_threshold=0.90),
        )
    )

    converged, latest = await run_convergence_monitor(
        incident_id="inc-mem-clean",
        affected_services=["cartservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
        required_clean_samples=2,
    )
    assert converged is True
    assert latest["cartservice.memory_saturation"] == 0.75
    assert latest["_meta"]["clean_samples"] == 2


@pytest.mark.asyncio
async def test_memory_ratio_above_threshold_resets():
    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    async def mock_sleep(_):
        pass

    async def mock_prometheus(query: str):
        if "container_memory_working_set_bytes" in query:
            return 0.95  # 95% memory ratio > 90% threshold
        if "container_cpu_usage" in query:
            return 0.40
        if "http_request_duration_seconds" in query:
            return 150.0
        if "http_requests_total" in query:
            return 0.001
        return 0.0

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=20,
            slo=ConvergenceSLOConfig(memory_saturation_threshold=0.90),
        )
    )

    converged, latest = await run_convergence_monitor(
        incident_id="inc-mem-fail",
        affected_services=["cartservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
    )
    assert converged is False
    assert latest.get("cartservice.memory_saturation") == 0.95


@pytest.mark.asyncio
async def test_queries_come_from_config():
    from shared.config.schemas import ConvergenceQueriesConfig

    recorded_queries = []

    async def mock_prometheus(query: str):
        recorded_queries.append(query)
        return 0.001

    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    async def mock_sleep(_):
        pass

    custom_queries = ConvergenceQueriesConfig(
        error_rate='custom_error_{service}_query',
        p99_latency_ms='custom_latency_{service}_query',
        cpu_saturation='custom_cpu_{service}_query',
        memory_saturation='custom_memory_{service}_query',
    )

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=10,
            slo=ConvergenceSLOConfig(),
            queries=custom_queries,
        )
    )

    converged, _ = await run_convergence_monitor(
        incident_id="inc-custom-queries",
        affected_services=["custom-svc"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
        required_clean_samples=1,
    )

    assert converged is True
    assert "custom_error_custom-svc_query" in recorded_queries
    assert "custom_latency_custom-svc_query" in recorded_queries
    assert "custom_cpu_custom-svc_query" in recorded_queries
    assert "custom_memory_custom-svc_query" in recorded_queries


@pytest.mark.asyncio
async def test_convergence_halts_and_resumes_on_system_pause():
    import json
    from unittest.mock import MagicMock

    prom_calls = 0

    async def mock_prometheus(query: str):
        nonlocal prom_calls
        prom_calls += 1
        return 0.001

    sleep_calls = []

    async def mock_sleep(seconds: float):
        sleep_calls.append(seconds)

    current_time = 1000.0

    def mock_now():
        nonlocal current_time
        current_time += 10.0
        return current_time

    # Pause is active for the first 2 checkpoint polls, then resumed
    pause_checks = 0

    def mock_redis_get(key: str):
        nonlocal pause_checks
        if key == "system:status:paused":
            pause_checks += 1
            if pause_checks <= 2:
                return json.dumps({"paused": True, "reason": "maintenance"}).encode("utf-8")
            return json.dumps({"paused": False}).encode("utf-8")
        return None

    fake_redis = MagicMock()
    fake_redis.get.side_effect = mock_redis_get

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=0,
            poll_interval_seconds=10,
            window_seconds=20,  # 2 clean samples required
            slo=ConvergenceSLOConfig(),
        )
    )

    converged, latest = await run_convergence_monitor(
        incident_id="inc-halt-resume",
        affected_services=["frontend"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        now_fn=mock_now,
        redis_client=fake_redis,
    )

    assert converged is True
    assert latest["frontend.error_rate"] == 0.001
    assert latest["_meta"]["clean_samples"] == 2
    # Two pause sleeps occurred before PromQL calls resumed and finished 2 clean samples
    assert 2.0 in sleep_calls
    assert prom_calls == 8  # 4 signals x 2 clean samples


@pytest.mark.asyncio
async def test_convergence_aborts_immediately_when_incident_dropped():
    from unittest.mock import MagicMock

    prom_calls = 0

    async def mock_prometheus(query: str):
        nonlocal prom_calls
        prom_calls += 1
        return 0.001

    async def mock_sleep(_):
        pass

    fake_redis = MagicMock()
    fake_redis.get.side_effect = lambda key: b"1" if key == "incident:inc-drop-mid:dropped" else None

    cfg = SystemConfig(
        convergence=ConvergenceConfig(
            bake_time_seconds_stateless=30,
            poll_interval_seconds=10,
            window_seconds=30,
            slo=ConvergenceSLOConfig(),
        )
    )

    converged, latest = await run_convergence_monitor(
        incident_id="inc-drop-mid",
        affected_services=["cartservice"],
        is_stateful=False,
        mcp_client=mock_prometheus,
        system_config=cfg,
        sleep_fn=mock_sleep,
        redis_client=fake_redis,
    )

    assert converged is False
    assert latest == {"aborted": "incident_dropped"}
    assert prom_calls == 0

