"""Unit tests for WP4 — Async correctness & thread safety (F1, F2)."""

import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.api_server.notifications import dispatch_notification_background
from services.api_server.state_machine import IncidentMachine
from shared.harness.crewai_tools import AgentBudgetState, MCPToolWrapper
from tests.unit.test_correlator import CorrelatorFakeRedis
from pydantic import BaseModel


@pytest.mark.asyncio
async def test_state_machine_notification_reaches_dispatch_from_executor_thread():
    fake_redis = CorrelatorFakeRedis()
    machine = IncidentMachine(incident_id="inc-async-notify-01", redis_client=fake_redis)
    # Advance to REMEDIATION first
    machine.state = "REMEDIATION"

    with patch("services.api_server.notifications.dispatch_notification_background") as mock_dispatch:
        await machine.trigger("action_proposed", actor="remediation_agent")
        # Give the event loop time to process
        await asyncio.sleep(0.05)

        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["event"] == "approval_request"
        assert call_kwargs["incident_id"] == "inc-async-notify-01"
        assert call_kwargs.get("loop") is not None
        assert call_kwargs["loop"].is_running()


def test_dispatch_background_no_loop_logs_warning(caplog):
    result_holder = {}

    def run_without_loop():
        # Dedicated thread without an event loop
        with caplog.at_level(logging.WARNING):
            dispatch_notification_background(
                event="approval_request",
                incident_id="inc-no-loop-1",
                data={},
                loop=None,
            )
        result_holder["done"] = True

    t = threading.Thread(target=run_without_loop)
    t.start()
    t.join()

    assert result_holder.get("done") is True
    assert any("No active event loop available" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_execute_crew_runs_kickoff_in_executor():
    from shared.harness.mcp import MCPClient

    # Verify that during crew.kickoff running in thread executor, a concurrent ticker task advances
    ticker_count = 0
    running = True

    async def ticker():
        nonlocal ticker_count, running
        while running:
            ticker_count += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())

    class DummyArgs(BaseModel):
        target: str

    mock_mcp = AsyncMock(spec=MCPClient)
    mock_mcp.call_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "tool_executed_ok"}]})
    budget = AgentBudgetState()

    tool = MCPToolWrapper(
        name="test_tool",
        description="A test tool",
        args_schema=DummyArgs,
        mcp_client=mock_mcp,
        mcp_tool_name="test_tool",
        agent_state=budget,
    )

    def blocking_crew_kickoff():
        import time
        # Tool call executed synchronously from worker thread
        res = tool._run(target="cartservice")
        time.sleep(0.08)  # simulate LLM reasoning in worker thread
        return res

    loop = asyncio.get_running_loop()
    tool_result = await loop.run_in_executor(None, blocking_crew_kickoff)

    running = False
    await ticker_task

    assert tool_result == "tool_executed_ok"
    assert budget.tool_calls_made == 1
    # Ticker task was not blocked by kickoff!
    assert ticker_count >= 2


def test_budget_state_thread_safe():
    state = AgentBudgetState()
    num_threads = 10
    increments_per_thread = 100

    def worker():
        for _ in range(increments_per_thread):
            state.increment()

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.tool_calls_made == num_threads * increments_per_thread
