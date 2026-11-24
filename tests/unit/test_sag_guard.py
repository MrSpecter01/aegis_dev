import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from shared.sag.check_approval import check_approval_status
from shared.harness.sag_guard import (
    SAGGuard,
    SAGActionBlockedError,
    SAGApprovalRequiredError,
)


@pytest.mark.asyncio
async def test_sag_guard_read_tool_passes_immediately():
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = {"content": "ok"}
    mock_redis = AsyncMock()

    guard = SAGGuard(mcp_client=mock_mcp, redis_client=mock_redis)

    res = await guard.call_tool(
        tool_name="prometheus_query",
        arguments={"query": "up"},
    )
    assert res == {"content": "ok"}
    # Read tool should not touch Redis
    mock_redis.get.assert_not_called()
    mock_mcp.call_tool.assert_called_once_with("prometheus_query", {"query": "up"})


@pytest.mark.asyncio
async def test_sag_guard_write_tool_approved_proceeds():
    mock_mcp = AsyncMock()
    mock_mcp.call_tool.return_value = {"success": True}
    mock_redis = AsyncMock()
    mock_redis.get.return_value = json.dumps({
        "status": "approved",
        "action_id": "act-123",
        "proposal": {"actions": [{"tool_name": "restart_pod"}]},
    })

    guard = SAGGuard(mcp_client=mock_mcp, redis_client=mock_redis)

    res = await guard.call_tool(
        tool_name="restart_pod",
        arguments={"namespace": "boutique", "pod_name": "cartservice-abc"},
        incident_id="inc-100",
        action_id="act-123",
    )
    assert res == {"success": True}
    mock_mcp.call_tool.assert_called_once()


@pytest.mark.asyncio
async def test_sag_guard_write_tool_pending_raises_approval_required():
    mock_mcp = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.rpush = MagicMock()
    mock_redis.get.return_value = json.dumps({"status": "pending", "action_id": "act-123"})

    guard = SAGGuard(mcp_client=mock_mcp, redis_client=mock_redis)

    with pytest.raises(SAGApprovalRequiredError, match="requires human approval"):
        await guard.call_tool(
            tool_name="restart_pod",
            arguments={"namespace": "boutique", "pod_name": "cartservice-abc"},
            incident_id="inc-100",
            action_id="act-123",
        )
    # MCP tool call should never have been dispatched
    mock_mcp.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_sag_guard_write_tool_denied_raises_blocked_and_logs_ledger():
    mock_mcp = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.rpush = MagicMock()
    mock_redis.get.return_value = json.dumps({
        "status": "denied",
        "action_id": "act-123",
        "actor": "sre@example.com",
    })

    guard = SAGGuard(mcp_client=mock_mcp, redis_client=mock_redis)

    with pytest.raises(SAGActionBlockedError, match="was DENIED"):
        await guard.call_tool(
            tool_name="scale_deployment",
            arguments={"namespace": "boutique", "deployment": "frontend", "replicas": 5},
            incident_id="inc-100",
            action_id="act-123",
        )

    mock_mcp.call_tool.assert_not_called()
    # Ledger should have block record appended
    mock_redis.rpush.assert_called()


@pytest.mark.asyncio
async def test_sag_guard_write_tool_missing_ids_raises():
    mock_mcp = AsyncMock()
    mock_redis = AsyncMock()

    guard = SAGGuard(mcp_client=mock_mcp, redis_client=mock_redis)

    with pytest.raises(SAGApprovalRequiredError, match="requires both 'incident_id' and 'action_id'"):
        await guard.call_tool(
            tool_name="drain_node",
            arguments={"node": "node-1", "grace_period_seconds": 120},
        )


@pytest.mark.asyncio
async def test_sag_guard_write_tool_injection_rejected():
    mock_mcp = AsyncMock()
    mock_redis = AsyncMock()

    guard = SAGGuard(mcp_client=mock_mcp, redis_client=mock_redis)

    with pytest.raises(ValueError, match="Prompt injection"):
        await guard.call_tool(
            tool_name="restart_pod",
            arguments={
                "namespace": "boutique",
                "pod_name": "cartservice; rm -rf /",
            },
            incident_id="inc-100",
            action_id="act-123",
        )


@pytest.mark.asyncio
async def test_check_approval_status_cases():
    mock_redis = AsyncMock()

    # Case 0: Approved
    mock_redis.get.return_value = json.dumps({"status": "approved"})
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 0

    # Case 1: Denied
    mock_redis.get.return_value = json.dumps({"status": "denied"})
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 1

    # Case 2: Pending
    mock_redis.get.return_value = json.dumps({"status": "pending"})
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 2

    # Case 1: Steered / Timed_out / Expired (treat as blocked)
    mock_redis.get.return_value = json.dumps({"status": "steered"})
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 1

    mock_redis.get.return_value = json.dumps({"status": "timed_out"})
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 1

    mock_redis.get.return_value = json.dumps({"status": "expired"})
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 1

    # Case 2: Missing record
    mock_redis.get.return_value = None
    assert await check_approval_status(mock_redis, "inc-1", "act-1") == 2
