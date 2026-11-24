"""Unit tests for shared/harness/crewai_tools.py.

Tests schema generation, MCP tool wrapping, sync/async runs, and budget tracking.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from pydantic import BaseModel

from shared.harness.crewai_tools import (
    AgentBudgetState,
    MCPToolWrapper,
    _build_pydantic_model_from_schema,
    build_mcp_tools,
)
from shared.harness.mcp import MCPClient


def test_agent_budget_state():
    state = AgentBudgetState()
    assert state.tool_calls_made == 0
    state.tool_calls_made += 1
    assert state.tool_calls_made == 1


def test_build_pydantic_model_from_schema_types():
    schema = {
        "properties": {
            "str_field": {"type": "string", "description": "a string"},
            "int_field": {"type": "integer", "description": "an integer"},
            "num_field": {"type": "number", "description": "a float"},
            "bool_field": {"type": "boolean", "description": "a boolean"},
            "arr_field": {"type": "array", "description": "a list"},
            "unknown_field": {"type": "unknown", "description": "any type"},
        },
        "required": ["str_field", "int_field"],
    }

    Model = _build_pydantic_model_from_schema(schema, "TestSchema")
    assert issubclass(Model, BaseModel)

    # Required fields must be provided
    with pytest.raises(Exception):
        Model(num_field=3.14)

    # Valid instantiation
    instance = Model(
        str_field="hello",
        int_field=42,
        num_field=3.14,
        bool_field=True,
        arr_field=["a", "b"],
        unknown_field={"custom": "data"},
    )
    assert instance.str_field == "hello"
    assert instance.int_field == 42
    assert instance.bool_field is True
    assert instance.arr_field == ["a", "b"]


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_arun_success_with_text_content():
    mock_client = MagicMock(spec=MCPClient)
    mock_client.call_tool = AsyncMock(
        return_value={
            "content": [
                {"type": "text", "text": "Pod restart count: 5"},
                {"type": "image", "data": "base64"},  # Non-text ignored
                {"type": "text", "text": "ExitCode: 137"},
            ]
        }
    )

    class DummyArgs(BaseModel):
        pod_name: str

    state = AgentBudgetState()
    tool = MCPToolWrapper(
        name="k8s_get_pod_status",
        description="Get pod status",
        args_schema=DummyArgs,
        mcp_client=mock_client,
        mcp_tool_name="k8s_get_pod_status",
        agent_state=state,
    )

    result = await tool._arun(pod_name="cartservice")

    assert state.tool_calls_made == 1
    assert "Pod restart count: 5\nExitCode: 137" in result
    mock_client.call_tool.assert_awaited_once_with(
        "k8s_get_pod_status", arguments={"pod_name": "cartservice"}
    )


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_arun_fallback_string_and_exception():
    mock_client = MagicMock(spec=MCPClient)
    # 1. Non-dict or dict without content
    mock_client.call_tool = AsyncMock(return_value={"status": "healthy"})

    class DummyArgs(BaseModel):
        pass

    state = AgentBudgetState()
    tool = MCPToolWrapper(
        name="health_check",
        description="Health check",
        args_schema=DummyArgs,
        mcp_client=mock_client,
        mcp_tool_name="health_check",
        agent_state=state,
    )

    result = await tool._arun()
    assert result == "{'status': 'healthy'}"
    assert state.tool_calls_made == 1

    # 2. Tool raises Exception
    mock_client.call_tool = AsyncMock(side_effect=RuntimeError("Connection refused"))
    err_result = await tool._arun()
    assert "Error executing tool health_check: Connection refused" in err_result
    assert state.tool_calls_made == 2


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_run_in_running_loop_raises_runtime_error():
    mock_client = MagicMock(spec=MCPClient)
    state = AgentBudgetState()

    class DummyArgs(BaseModel):
        pass

    tool = MCPToolWrapper(
        name="test_tool",
        description="test",
        args_schema=DummyArgs,
        mcp_client=mock_client,
        mcp_tool_name="test_tool",
        agent_state=state,
    )

    # Calling synchronous _run from inside an active asyncio loop must raise RuntimeError
    with pytest.raises(RuntimeError, match="Called sync _run from an async context"):
        tool._run()


def test_mcp_tool_wrapper_run_sync_execution():
    mock_client = MagicMock(spec=MCPClient)
    mock_client.call_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "sync ok"}]})
    state = AgentBudgetState()

    class DummyArgs(BaseModel):
        pass

    tool = MCPToolWrapper(
        name="test_tool",
        description="test",
        args_schema=DummyArgs,
        mcp_client=mock_client,
        mcp_tool_name="test_tool",
        agent_state=state,
    )

    # Run in a synchronous context (no running loop)
    res = tool._run()
    assert res == "sync ok"
    assert state.tool_calls_made == 1


@pytest.mark.asyncio
async def test_build_mcp_tools():
    mock_client = MagicMock(spec=MCPClient)
    mock_client.list_tools = AsyncMock(
        return_value=[
            {
                "name": "prometheus_query",
                "description": "Run PromQL query",
                "inputSchema": {
                    "properties": {
                        "query": {"type": "string", "description": "PromQL expression"}
                    },
                    "required": ["query"],
                },
            }
        ]
    )

    state = AgentBudgetState()
    tools = await build_mcp_tools(mock_client, state)

    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "prometheus_query"
    assert tool.description == "Run PromQL query"
    assert tool.mcp_client == mock_client
    assert tool.agent_state == state


def test_build_pydantic_model_from_fastmcp_anyof_optional():
    """FastMCP anyOf/oneOf nullable schemas must preserve underlying primitive types instead of collapsing to Any."""
    schema = {
        "properties": {
            "flag_name": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Optional flag name",
            },
            "since_minutes": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Lookback window",
            },
            "include_logs": {
                "oneOf": [{"type": "boolean"}, {"type": "null"}],
                "description": "Boolean toggle",
            },
        },
        "required": [],
    }

    Model = _build_pydantic_model_from_schema(schema, "FastMCPOptionalArgs")
    valid = Model(flag_name="cartFailure", since_minutes=15, include_logs=True)
    assert valid.flag_name == "cartFailure"
    assert valid.since_minutes == 15
    assert valid.include_logs is True

    valid_none = Model()
    assert valid_none.flag_name is None

    with pytest.raises(Exception):
        Model(since_minutes="not-an-integer")
