import os
import pytest
from pathlib import Path

from shared.config import (
    load_system_config,
    load_agents_config,
    load_mcp_config,
    load_sag_policies_config,
    ConfigManager,
)


def test_load_default_configs():
    sys_cfg = load_system_config()
    assert sys_cfg.system.api_port == 8080
    assert sys_cfg.redis.url in ["redis://localhost:6379", "redis://redis:6379"]
    assert sys_cfg.qdrant.embedding_provider in ["ollama", "gemini"]
    assert sys_cfg.qdrant.embedding_model in ["qwen3-embedding:0.6b-q8_0", "gemini-embedding-2"]
    assert sys_cfg.convergence.window_seconds in [60, 300]
    assert sys_cfg.convergence.slo.error_rate_threshold == 0.01
    assert sys_cfg.evaluation.judge.provider == "gemini"
    assert sys_cfg.evaluation.judge.max_calls_per_run == 50
    assert sys_cfg.evaluation.judge.rate_limit_rpm == 15

    agents_cfg = load_agents_config()
    assert "infrastructure_investigator" in agents_cfg.agents
    infra_agent = agents_cfg.agents["infrastructure_investigator"]
    assert infra_agent.primary.provider == "gemini"
    assert infra_agent.primary.model.startswith("gemini")

    mcp_cfg = load_mcp_config()
    assert "prometheus" in mcp_cfg.mcp_servers
    assert "prometheus_query" in mcp_cfg.mcp_servers["prometheus"].tools
    assert "9000" in mcp_cfg.mcp_servers["qdrant"].server_url

    sag_cfg = load_sag_policies_config()
    assert sag_cfg.confidence_thresholds["P1"] in [0.85, 0.90]
    assert sag_cfg.max_retries["P2"] == 3


def test_config_manager():
    manager = ConfigManager()
    assert manager.system is not None
    assert manager.agents is not None
    assert manager.mcp is not None
    assert manager.sag is not None


def test_env_var_overrides(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://custom-redis:6379")
    monkeypatch.setenv("OLLAMA_URL", "http://custom-ollama:11434")

    sys_cfg = load_system_config()
    assert sys_cfg.redis.url == "redis://custom-redis:6379"
    assert sys_cfg.ollama.url == "http://custom-ollama:11434"


def test_mcp_config_env_expansion_auth_and_external_preservation(tmp_path, monkeypatch):
    from shared.config.schemas import MCPServerConfig

    mcp_yaml = tmp_path / "mcp.yaml"
    mcp_yaml.write_text(
        """
mcp_servers:
  prometheus:
    server_url: "http://aegis-prometheus-mcp:9001/mcp"
    tools:
      - prometheus_query
  topology:
    server_url: "http://aegis-topology-mcp:9008/mcp"
    tools:
      - topology_query_node
  external_sse:
    server_url: "https://mcp.example.com/sse"
    transport: "sse"
    external: true
    auth_type: "api_key"
    auth_header: "X-MCP-Key"
    api_key: "${EXT_MCP_KEY}"
    headers:
      X-Tenant: "${TENANT_ID:-prod}"
    tools:
      - ext_query
  env_token_http:
    server_url: "https://internal-ext.example.com/mcp"
    api_key_env: "SECOND_MCP_TOKEN"
    tools:
      - internal_ext_tool
  local_stdio:
    transport: "stdio"
    command: "python"
    args: ["-m", "some_mcp"]
    env:
      API_TOKEN: "${EXT_MCP_KEY}"
    tools:
      - stdio_tool
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("EXT_MCP_KEY", "secret-123")
    monkeypatch.setenv("SECOND_MCP_TOKEN", "secret-456")
    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.setenv("AEGIS_MCP_URL", "http://aegis-mcp:9000/mcp")

    cfg = load_mcp_config(mcp_yaml)

    # Internal Aegis HTTP service gets rewritten to AEGIS_MCP_URL; topology stays untouched
    assert cfg.mcp_servers["prometheus"].server_url == "http://aegis-mcp:9000/mcp"
    assert cfg.mcp_servers["topology"].server_url == "http://aegis-topology-mcp:9008/mcp"

    # External SSE server preserves URL, expands env vars, and applies default
    ext_sse = cfg.mcp_servers["external_sse"]
    assert ext_sse.server_url == "https://mcp.example.com/sse"
    assert ext_sse.transport == "sse"
    assert ext_sse.api_key == "secret-123"
    assert ext_sse.auth_header == "X-MCP-Key"
    assert ext_sse.headers == {"X-Tenant": "prod"}

    # External URL (without aegis- prefix) + api_key_env works without AEGIS_MCP_URL overwrite
    env_tok = cfg.mcp_servers["env_token_http"]
    assert env_tok.server_url == "https://internal-ext.example.com/mcp"
    assert env_tok.api_key == "secret-456"

    # STDIO server preserves command/args and expands env dict
    stdio_srv = cfg.mcp_servers["local_stdio"]
    assert stdio_srv.transport == "stdio"
    assert stdio_srv.command == "python"
    assert stdio_srv.args == ["-m", "some_mcp"]
    assert stdio_srv.env == {"API_TOKEN": "secret-123"}

    # Validation checks
    with pytest.raises(ValueError, match="server_url is required"):
        MCPServerConfig(transport="sse", tools=["x"])
    with pytest.raises(ValueError, match="command is required"):
        MCPServerConfig(transport="stdio", tools=["x"])
