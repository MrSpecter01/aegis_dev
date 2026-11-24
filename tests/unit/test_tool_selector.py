import pytest
from unittest.mock import AsyncMock, MagicMock

from shared.config.schemas import MCPServerConfig, ToolItemConfig
from shared.models.enums import AgentRole
from services.phase1_worker.agents.base import BaseInvestigator


def test_mcp_server_config_tool_tags_and_names():
    server = MCPServerConfig(
        server_url="http://mock-jaeger:9009/mcp",
        tools=[
            ToolItemConfig(name="jaeger_search_traces", tags=["traces", "search", "read"]),
            ToolItemConfig(name="jaeger_get_trace", tags=["traces", "read"]),
            "legacy_string_tool",
        ],
    )

    assert "jaeger_search_traces" in server.tool_names
    assert "jaeger_get_trace" in server.tool_names
    assert "legacy_string_tool" in server.tool_names
    assert "jaeger_search_traces" in server  # tests __contains__

    assert server.get_tool_tags("jaeger_search_traces") == ["traces", "search", "read"]
    assert server.get_tool_tags("jaeger_get_trace") == ["traces", "read"]
    assert server.get_tool_tags("legacy_string_tool") == []


@pytest.mark.asyncio
async def test_agent_tool_selector_scoping(monkeypatch):
    """Test that BaseInvestigator scopes tools strictly using tool_selector tags."""
    # Mock LLM and Crew
    mock_llm = MagicMock()
    mock_llm.model = "gemini-3.5-flash-lite"

    # Mock MCP client with 3 tools: jaeger (traces/read), loki (logs/read), and restart_pod (k8s/write)
    mock_mcp_client = MagicMock()
    
    t_trace = MagicMock()
    t_trace.name = "jaeger_search_traces"

    t_log = MagicMock()
    t_log.name = "loki_query"

    t_write = MagicMock()
    t_write.name = "restart_pod"

    async def mock_build_mcp_tools(client, state):
        return [t_trace, t_log, t_write]

    monkeypatch.setattr("services.phase1_worker.agents.base.build_mcp_tools", mock_build_mcp_tools)

    # Initialize investigator for log_trace_investigator (selector: match_any: [logs, traces], exclude: [write])
    inv = BaseInvestigator(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        agent_config_key="log_trace_investigator",
        task_config_key="investigate_logs_traces",
        llm=mock_llm,
        mcp_clients={"unified": mock_mcp_client},
    )

    # Mock Agent and Crew kickoff to intercept agent tools
    captured_tools = []

    def mock_agent_init(*args, **kwargs):
        nonlocal captured_tools
        captured_tools = list(kwargs.get("tools", []))
        return MagicMock()

    monkeypatch.setattr("services.phase1_worker.agents.base.Agent", mock_agent_init)
    monkeypatch.setattr("services.phase1_worker.agents.base.Task", lambda *a, **kw: MagicMock())

    def mock_crew_init(*args, **kwargs):
        mock_crew = MagicMock()
        mock_finding = MagicMock()
        mock_finding.evidence = []
        mock_finding.confidence = 0.5
        mock_finding.infra_flags = {}
        mock_finding.hypothesis = "test"
        mock_crew.kickoff.return_value = MagicMock(pydantic=mock_finding)
        return mock_crew

    monkeypatch.setattr("services.phase1_worker.agents.base.Crew", mock_crew_init)

    await inv._execute_crew(context_vars={"alert_service": "ad"})

    tool_names = [t.name for t in captured_tools]
    # Both traces and logs tools should be matched
    assert "jaeger_search_traces" in tool_names
    assert "loki_query" in tool_names
    # Write tool must be excluded
    assert "restart_pod" not in tool_names
