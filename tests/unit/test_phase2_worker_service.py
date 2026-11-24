import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import redis

from services.phase2_worker.main import Phase2WorkerService, STREAM_NAME, CONSUMER_GROUP


@pytest.fixture
def mock_redis():
    client = MagicMock(spec=redis.Redis)
    storage = {}

    def mock_get(key):
        return storage.get(key)

    def mock_set(key, val):
        storage[key] = val
        return True

    client.get.side_effect = mock_get
    client.set.side_effect = mock_set
    client.xack.return_value = 1
    client.storage = storage
    return client


def test_init_stream_group_success_and_busygroup(mock_redis):
    service = Phase2WorkerService(redis_client=mock_redis)

    # First call: success
    service.init_stream_group()
    mock_redis.xgroup_create.assert_called_with(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)

    # Second call: BUSYGROUP error
    mock_redis.xgroup_create.side_effect = redis.ResponseError("BUSYGROUP Consumer Group name already exists")
    service.init_stream_group()  # Should not raise


@pytest.mark.asyncio
async def test_post_transition_success_and_failure(mock_redis):
    service = Phase2WorkerService(redis_client=mock_redis)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        # Success 200
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"status": "ok"})
        ok = await service._post_transition("inc-1", "action_proposed", {"key": "val"})
        assert ok is True

        # Failure 500
        mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")
        fail = await service._post_transition("inc-1", "action_proposed")
        assert fail is False

        # Exception
        mock_post.side_effect = RuntimeError("Network down")
        err = await service._post_transition("inc-1", "action_proposed")
        assert err is False


@pytest.mark.asyncio
async def test_process_job_action_proposed(mock_redis):
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "outcome": "action_proposed",
            "action_proposal": {"incident_id": "inc-test-1", "confidence": 0.85},
            "pre_approved": False,
        }
    )

    service = Phase2WorkerService(redis_client=mock_redis, graph=mock_graph)
    service._post_transition = AsyncMock(return_value=True)

    await service.process_job("1-0", {"incident_id": "inc-test-1"})

    # Proposal written to Redis
    assert "incident:inc-test-1:proposal" in mock_redis.storage
    action_saved = json.loads(mock_redis.storage["incident:inc-test-1:proposal"])
    assert action_saved["confidence"] == 0.85

    # Transition triggered
    service._post_transition.assert_called_once()
    call_args = service._post_transition.call_args[0]
    assert call_args[0] == "inc-test-1"
    assert call_args[1] == "action_proposed"

    # Message ACKed
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "1-0")


@pytest.mark.asyncio
async def test_process_job_signals_converged(mock_redis):
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "outcome": "signals_converged",
            "action_proposal": {"incident_id": "inc-test-2"},
            "convergence_result": {"converged": True},
        }
    )

    service = Phase2WorkerService(redis_client=mock_redis, graph=mock_graph)
    service._post_transition = AsyncMock(return_value=True)

    await service.process_job("2-0", {"incident_id": "inc-test-2"})

    service._post_transition.assert_called_once()
    call_args = service._post_transition.call_args[0]
    assert call_args[0] == "inc-test-2"
    assert call_args[1] == "signals_converged"


@pytest.mark.asyncio
async def test_process_job_signals_not_converged_retry_and_exhaustion(mock_redis):
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "outcome": "signals_not_converged",
            "action_proposal": {"incident_id": "inc-test-3"},
            "signals_at_timeout": {"error_rate": 0.04},
        }
    )

    service = Phase2WorkerService(redis_client=mock_redis, graph=mock_graph)
    service._post_transition = AsyncMock(return_value=True)

    # Initial state in Redis: P2 severity (max retries = 3)
    mock_redis.storage["incident:inc-test-3:state"] = json.dumps({"severity": "P2"})

    # Attempt 1: retry under limit
    await service.process_job("3-0", {"incident_id": "inc-test-3", "retry_count": 0})
    service._post_transition.assert_called_with(
        "inc-test-3",
        "signals_not_converged",
        metadata={
            "prior_attempts_count": 1,
            "signals_at_timeout": {"error_rate": 0.04},
            "convergence": None,
        },
    )

    # Verify prior attempts appended
    prior = json.loads(mock_redis.storage["incident:inc-test-3:prior_attempts"])
    assert len(prior) == 1
    assert prior[0]["signals_at_timeout"] == {"error_rate": 0.04}

    # Simulate existing 2 prior attempts, so attempt 3 reaches limit
    mock_redis.storage["incident:inc-test-3:prior_attempts"] = json.dumps([{"attempt": 1}, {"attempt": 2}])
    await service.process_job("3-1", {"incident_id": "inc-test-3", "retry_count": 2})

    call_args = service._post_transition.call_args[0]
    assert call_args[0] == "inc-test-3"
    assert call_args[1] == "retry_limit_reached"


@pytest.mark.asyncio
async def test_process_job_low_confidence_escalation(mock_redis):
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "outcome": "low_confidence",
            "action_proposal": None,
        }
    )

    service = Phase2WorkerService(redis_client=mock_redis, graph=mock_graph)
    service._post_transition = AsyncMock(return_value=True)

    await service.process_job("4-0", {"incident_id": "inc-test-4"})

    service._post_transition.assert_called_once()
    call_args = service._post_transition.call_args[0]
    assert call_args[0] == "inc-test-4"
    assert call_args[1] == "escalate"


@pytest.mark.asyncio
async def test_process_job_missing_incident_id(mock_redis):
    service = Phase2WorkerService(redis_client=mock_redis)
    service._post_transition = AsyncMock()

    await service.process_job("5-0", {})
    service._post_transition.assert_not_called()
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "5-0")


@pytest.mark.asyncio
async def test_process_job_unhandled_exception_escalates(mock_redis):
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("Graph crash"))

    service = Phase2WorkerService(redis_client=mock_redis, graph=mock_graph)
    service._post_transition = AsyncMock(return_value=True)

    await service.process_job("6-0", {"incident_id": "inc-test-6"})

    service._post_transition.assert_called_once()
    call_args = service._post_transition.call_args[0]
    assert call_args[0] == "inc-test-6"
    assert call_args[1] == "escalate"


@pytest.mark.asyncio
async def test_worker_run_and_stop(mock_redis):
    service = Phase2WorkerService(redis_client=mock_redis)
    service.process_job = AsyncMock()

    # Simulate 1 stream message returned, then stop
    mock_redis.xreadgroup.side_effect = [
        [(STREAM_NAME, [("msg-10", {"incident_id": "inc-10"})])],
        [],
    ]

    async def auto_stop():
        await asyncio.sleep(0.05)
        service.stop()

    asyncio.create_task(auto_stop())
    await service.run()

    service.process_job.assert_called_once_with("msg-10", {"incident_id": "inc-10"})
    assert service.running is False
