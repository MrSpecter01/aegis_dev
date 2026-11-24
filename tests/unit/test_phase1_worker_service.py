"""Unit tests for services/phase1_worker/main.py.

Tests stream group initialization, job consumption, error escalation,
and API state transitions.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import redis

from services.phase1_worker.main import Phase1WorkerService, STREAM_NAME, CONSUMER_GROUP
from shared.models.findings import MergedFinding


@pytest.fixture
def mock_redis():
    client = MagicMock(spec=redis.Redis)
    client.xgroup_create = MagicMock()
    client.xack = MagicMock()
    client.xreadgroup = MagicMock()
    client.get = MagicMock(return_value=None)
    return client


def test_init_stream_group_success(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker.init_stream_group()
    mock_redis.xgroup_create.assert_called_once_with(
        STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True
    )


def test_init_stream_group_busygroup_handled(mock_redis):
    mock_redis.xgroup_create.side_effect = redis.ResponseError("BUSYGROUP Consumer Group name already exists")
    worker = Phase1WorkerService(redis_client=mock_redis)
    # Should not raise exception
    worker.init_stream_group()


def test_init_stream_group_other_error_raises(mock_redis):
    mock_redis.xgroup_create.side_effect = redis.ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")
    worker = Phase1WorkerService(redis_client=mock_redis)
    with pytest.raises(redis.ResponseError):
        worker.init_stream_group()


@pytest.mark.asyncio
async def test_post_transition_success():
    worker = Phase1WorkerService(api_url="http://localhost:8000", api_key="secret")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok"}

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        ok = await worker._post_transition("inc-123", "triage_complete")
        assert ok is True


@pytest.mark.asyncio
async def test_post_transition_non_200_returns_false():
    worker = Phase1WorkerService(api_url="http://localhost:8000", api_key="secret")
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Invalid transition"

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        ok = await worker._post_transition("inc-123", "triage_complete")
        assert ok is False


@pytest.mark.asyncio
async def test_post_transition_exception_returns_false():
    worker = Phase1WorkerService(api_url="http://localhost:8000", api_key="secret")

    with patch("httpx.AsyncClient.post", AsyncMock(side_effect=Exception("Connection refused"))):
        ok = await worker._post_transition("inc-123", "triage_complete")
        assert ok is False


@pytest.mark.asyncio
async def test_process_job_missing_incident_id(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    await worker.process_job("msg-1", {"some_key": "some_value"})
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "msg-1")


@pytest.mark.asyncio
async def test_process_job_string_metadata_parsing(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker._post_transition = AsyncMock(return_value=True)

    sample_merged = MergedFinding(
        incident_id="inc-parsed-01",
        primary_hypothesis="Test",
        merged_confidence=0.8,
        findings_by_agent={},
        all_evidence=[],
        verbatim_signals={},
        infra_flags={},
        corroboration_score=0.7,
    )

    with patch("services.phase1_worker.main.Phase1Flow") as MockFlow, \
         patch("services.phase1_worker.main.get_incident", return_value={"affected_service": "cart", "affected_namespace": "default"}):
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=sample_merged)
        MockFlow.return_value = mock_instance

        job_data = {
            "incident_id": "inc-parsed-01",
            "alert_metadata": json.dumps({"severity": "P1"}),
            "topology_subgraph": json.dumps({"nodes": []}),
        }

        await worker.process_job("msg-2", job_data)

    assert MockFlow.call_args[1]["alert_metadata"] == {"severity": "P1"}
    assert MockFlow.call_args[1]["topology_subgraph"] == {"nodes": []}
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "msg-2")


@pytest.mark.asyncio
async def test_process_job_flow_failure_escalates(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    transition_calls = []

    async def mock_transition(inc_id, event, metadata=None):
        transition_calls.append((inc_id, event, metadata))
        return True

    worker._post_transition = mock_transition

    with patch("services.phase1_worker.main.Phase1Flow") as MockFlow:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(side_effect=RuntimeError("Agents crashed"))
        MockFlow.return_value = mock_instance

        job_data = {"incident_id": "inc-crash-01"}
        await worker.process_job("msg-3", job_data)

    assert len(transition_calls) == 1
    assert transition_calls[0][0] == "inc-crash-01"
    assert transition_calls[0][1] == "escalate"
    assert "Agents crashed" in transition_calls[0][2]["error"]
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "msg-3")


@pytest.mark.asyncio
async def test_worker_run_and_stop(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker.init_stream_group = MagicMock()

    # First iteration: returns 1 message, second iteration: worker stops
    messages = [("msg-10", {"incident_id": "inc-run-01"})]
    mock_redis.xreadgroup.side_effect = [
        [(STREAM_NAME, messages)],
        [],
    ]

    worker.process_job = AsyncMock()

    async def stop_after_delay():
        await asyncio.sleep(0.05)
        worker.stop()

    asyncio.create_task(stop_after_delay())
    await worker.run()

    assert worker.running is False
    worker.process_job.assert_awaited_once_with("msg-10", {"incident_id": "inc-run-01"})


@pytest.mark.asyncio
async def test_process_job_invalid_json_strings_handled(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker._post_transition = AsyncMock(return_value=True)

    with patch("services.phase1_worker.main.Phase1Flow") as MockFlow:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=MagicMock())
        MockFlow.return_value = mock_instance

        job_data = {
            "incident_id": "inc-bad-json",
            "alert_metadata": "not-valid-json{",
            "topology_subgraph": "not-valid-json{",
        }
        await worker.process_job("msg-4", job_data)

    assert MockFlow.call_args[1]["alert_metadata"] == {}
    assert MockFlow.call_args[1]["topology_subgraph"] == {}


@pytest.mark.asyncio
async def test_process_job_xack_exception_handled(mock_redis):
    mock_redis.xack.side_effect = RuntimeError("xack failed")
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker._post_transition = AsyncMock(return_value=True)

    with patch("services.phase1_worker.main.Phase1Flow") as MockFlow:
        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=MagicMock())
        MockFlow.return_value = mock_instance

        # Should complete and not raise exception
        await worker.process_job("msg-5", {"incident_id": "inc-xack-err"})


@pytest.mark.asyncio
async def test_worker_run_cancellation_and_error(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker.init_stream_group = MagicMock()

    # Iteration 1: raises generic Exception, Iteration 2: raises CancelledError
    call_count = 0

    def mock_xreadgroup(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Transient network hiccup")
        raise asyncio.CancelledError()

    mock_redis.xreadgroup = mock_xreadgroup

    with patch("asyncio.sleep", AsyncMock()):
        await worker.run()

    assert call_count >= 2


@pytest.mark.asyncio
async def test_main_service_entrypoint():
    from services.phase1_worker.main import main

    with patch("services.phase1_worker.main.Phase1WorkerService") as MockService, \
         patch("signal.signal") as mock_signal:
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        MockService.return_value = mock_worker

        await main()

        mock_worker.run.assert_awaited_once()
        assert mock_signal.call_count >= 2
        # Test signal handler
        handler = mock_signal.call_args_list[0][0][1]
        handler()
        mock_worker.stop.assert_called_once()


@pytest.mark.asyncio
async def test_process_job_fallback_resolution_from_redis(mock_redis):
    worker = Phase1WorkerService(redis_client=mock_redis)
    worker._post_transition = AsyncMock(return_value=True)

    fake_alert_group = {
        "severity": "P2",
        "primary_alert": {"labels": {"alertname": "PodMemoryHigh"}, "startsAt": "2026-10-23T09:09:21Z"},
    }

    def mock_get(key):
        if key == "incident:inc-fb-01:alert_group":
            return json.dumps(fake_alert_group)
        return None

    mock_redis.get.side_effect = mock_get

    sample_merged = MergedFinding(
        incident_id="inc-fb-01",
        primary_hypothesis="Resolved from Redis",
        merged_confidence=0.9,
        findings_by_agent={},
        all_evidence=[],
        verbatim_signals={},
        infra_flags={},
        corroboration_score=0.8,
    )

    with patch("services.phase1_worker.main.Phase1Flow") as MockFlow, \
         patch("services.phase1_worker.main.get_incident", return_value={"affected_service": "cartservice", "affected_namespace": "boutique", "severity": "P2"}), \
         patch("services.api_server.topology.store.TopologyStore") as MockStore, \
         patch("services.api_server.topology.query.get_subgraph") as mock_get_subgraph:
        
        mock_subgraph = MagicMock()
        mock_subgraph.model_dump.return_value = {"target_node": {"name": "cartservice"}}
        mock_get_subgraph.return_value = mock_subgraph

        mock_instance = MagicMock()
        mock_instance.run = AsyncMock(return_value=sample_merged)
        MockFlow.return_value = mock_instance

        job_data = {"incident_id": "inc-fb-01"}
        await worker.process_job("msg-fb-1", job_data)

    call_kwargs = MockFlow.call_args[1]
    assert call_kwargs["alert_metadata"]["severity"] == "P2"
    assert call_kwargs["alert_metadata"]["primary_alert"]["labels"]["alertname"] == "PodMemoryHigh"
    assert call_kwargs["topology_subgraph"] == {"target_node": {"name": "cartservice"}}
