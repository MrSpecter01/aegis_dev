"""Unit tests for Phase 3 Scribe Worker service daemon."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import redis

from services.phase3_worker.main import (
    Phase3WorkerService,
    STREAM_NAME,
    CONSUMER_GROUP,
)


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
    """Test consumer group initialization and BUSYGROUP handling."""
    service = Phase3WorkerService(redis_client=mock_redis)

    # First call: success
    service.init_stream_group()
    mock_redis.xgroup_create.assert_called_with(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)

    # Second call: BUSYGROUP error
    mock_redis.xgroup_create.side_effect = redis.ResponseError("BUSYGROUP Consumer Group name already exists")
    service.init_stream_group()  # Should not raise


@pytest.mark.asyncio
async def test_process_job_success(mock_redis):
    """Test successful job processing and stream message acknowledgment."""
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "incident_id": "inc-test-1",
            "is_valid": True,
            "delivery_result": {"transition_successful": True},
        }
    )

    service = Phase3WorkerService(redis_client=mock_redis, graph=mock_graph)
    await service.process_job("1-0", {"incident_id": "inc-test-1"})

    mock_graph.ainvoke.assert_called_once_with(
        {
            "incident_id": "inc-test-1",
            "retry_count": 0,
        },
        config={"configurable": {"thread_id": "inc-test-1"}},
    )
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "1-0")


@pytest.mark.asyncio
async def test_process_job_missing_incident_id(mock_redis):
    """Test job with missing incident_id is gracefully ACKed without graph execution."""
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock()

    service = Phase3WorkerService(redis_client=mock_redis, graph=mock_graph)
    await service.process_job("2-0", {})

    mock_graph.ainvoke.assert_not_called()
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "2-0")


@pytest.mark.asyncio
async def test_process_job_graph_exception_handled(mock_redis):
    """Test job processing error is caught and message is still ACKed."""
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("Graph node failed"))

    service = Phase3WorkerService(redis_client=mock_redis, graph=mock_graph)
    await service.process_job("3-0", {"incident_id": "inc-fail-1"})

    mock_graph.ainvoke.assert_called_once()
    mock_redis.xack.assert_called_once_with(STREAM_NAME, CONSUMER_GROUP, "3-0")
