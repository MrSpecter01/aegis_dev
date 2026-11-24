"""Unit tests for the Aegis Command Line Interface (CLI).

Tests:
- aegis init: helm directory parsing, Redis persistence, and snapshot saving
- aegis seed runbooks: Qdrant seeding and TOC compilation
- aegis seed prompts: positive validation and missing file detection with clear error reporting
- aegis seed alert-rules: Prometheus alerting rules syntax validation and reload dispatch
- aegis seed mcp-config: inspection of registered MCP servers and tools
- aegis verify: health checks across Redis, Qdrant, Ollama, and MCP servers
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis
from typer.testing import CliRunner

from services.api_server.cli import app, REQUIRED_PROMPT_FILES

runner = CliRunner()


# -----------------------------------------------------------------------------
# aegis init tests
# -----------------------------------------------------------------------------
def test_cli_init_missing_directory(tmp_path):
    non_existent = tmp_path / "does_not_exist"
    result = runner.invoke(app, ["init", "--helm-dir", str(non_existent)])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_cli_init_success_with_redis(tmp_path):
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    # Add a sample deployment
    dep_file = chart_dir / "sample.yaml"
    dep_file.write_text("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-service
  namespace: boutique
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: test-service
    spec:
      containers:
        - name: app
          image: test:v1
          ports:
            - containerPort: 8080
""", encoding="utf-8")

    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    with patch("redis.from_url", return_value=mock_redis), \
         patch("services.api_server.cli.TopologyStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store

        result = runner.invoke(app, ["init", "--helm-dir", str(chart_dir), "--namespace", "boutique"])
        assert result.exit_code == 0
        assert "Topology initialized successfully" in result.output
        assert "Total Nodes: 1" in result.output
        mock_store.save.assert_called_once()


def test_cli_init_success_without_redis_snapshot_fallback(tmp_path):
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    snapshot_file = tmp_path / "data" / "topology_snapshot.json"

    with patch("redis.from_url", side_effect=Exception("Redis connection refused")), \
         patch("services.api_server.cli.get_config") as mock_cfg:
        cfg = MagicMock()
        cfg.system.topology.helm_dir = str(chart_dir)
        cfg.system.topology.snapshot_path = str(snapshot_file)
        cfg.system.redis.url = "redis://localhost:6379"
        cfg.system.redis.db = 0
        mock_cfg.return_value = cfg

        result = runner.invoke(app, ["init", "--helm-dir", str(chart_dir)])
        assert result.exit_code == 0
        assert "Warning: Could not connect to Redis" in result.output
        assert "Topology initialized successfully" in result.output


# -----------------------------------------------------------------------------
# aegis seed runbooks tests
# -----------------------------------------------------------------------------
def test_cli_seed_runbooks_success():
    with patch("services.api_server.cli.seed_qdrant_runbooks", new_callable=AsyncMock) as mock_seed:
        mock_seed.return_value = 14  # 14 chunks seeded
        result = runner.invoke(app, ["seed", "runbooks", "--strict"])
        assert result.exit_code == 0
        assert "Successfully seeded 14 runbook chunk(s) into Qdrant" in result.output
        mock_seed.assert_called_once()


def test_cli_seed_runbooks_force_flag():
    with patch("services.api_server.cli.seed_qdrant_runbooks", new_callable=AsyncMock) as mock_seed:
        mock_seed.return_value = 37
        result = runner.invoke(app, ["seed", "runbooks", "--force"])
        assert result.exit_code == 0
        assert "Successfully seeded 37 runbook chunk(s) into Qdrant" in result.output
        assert mock_seed.call_args.kwargs["force"] is True


def test_cli_seed_runbooks_missing_directory(tmp_path):
    non_existent = tmp_path / "no_runbooks"
    result = runner.invoke(app, ["seed", "runbooks", "--runbooks-dir", str(non_existent)])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_cli_seed_runbooks_qdrant_error():
    with patch("services.api_server.cli.seed_qdrant_runbooks", new_callable=AsyncMock) as mock_seed:
        mock_seed.side_effect = RuntimeError("Failed to connect to Qdrant cluster")
        result = runner.invoke(app, ["seed", "runbooks"])
        assert result.exit_code == 1
        assert "Error seeding runbooks into Qdrant" in result.output


# -----------------------------------------------------------------------------
# aegis seed prompts tests
# -----------------------------------------------------------------------------
def test_cli_seed_prompts_success(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    # Create all required prompt files
    for fname in REQUIRED_PROMPT_FILES:
        (prompts_dir / fname).write_text(f"# Content of {fname}\n", encoding="utf-8")

    # Add valid YAML content for agents.yaml and tasks.yaml
    (prompts_dir / "agents.yaml").write_text("""
infrastructure_investigator:
  role: Infrastructure Investigator
log_trace_investigator:
  role: Log & Trace Investigator
change_config_investigator:
  role: Change & Config Investigator
""", encoding="utf-8")

    (prompts_dir / "tasks.yaml").write_text("""
investigate_infrastructure:
  description: Investigate infra
investigate_logs_traces:
  description: Investigate logs
investigate_changes_configs:
  description: Investigate changes
""", encoding="utf-8")

    result = runner.invoke(app, ["seed", "prompts", "--prompts-dir", str(prompts_dir)])
    assert result.exit_code == 0
    assert "validated successfully" in result.output


def test_cli_seed_prompts_missing_file_reports_clear_error(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    # Create all EXCEPT system_remediation_agent.md and convergence_eval_template.md
    for fname in REQUIRED_PROMPT_FILES:
        if fname not in ("system_remediation_agent.md", "convergence_eval_template.md"):
            (prompts_dir / fname).write_text("content", encoding="utf-8")

    result = runner.invoke(app, ["seed", "prompts", "--prompts-dir", str(prompts_dir)])
    assert result.exit_code == 1
    assert "Missing or invalid required prompt file(s)" in result.output
    assert "system_remediation_agent.md" in result.output
    assert "convergence_eval_template.md" in result.output


def test_cli_seed_prompts_empty_file_reports_error(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    for fname in REQUIRED_PROMPT_FILES:
        (prompts_dir / fname).write_text("non-empty", encoding="utf-8")

    # Make one empty
    (prompts_dir / "incident_report_template.md").write_text("", encoding="utf-8")

    result = runner.invoke(app, ["seed", "prompts", "--prompts-dir", str(prompts_dir)])
    assert result.exit_code == 1
    assert "incident_report_template.md (empty file)" in result.output


# -----------------------------------------------------------------------------
# aegis seed alert-rules tests
# -----------------------------------------------------------------------------
def test_cli_seed_alert_rules_success(tmp_path):
    rules_file = tmp_path / "test-rules.yaml"
    rules_file.write_text("""
groups:
  - name: test_group
    rules:
      - alert: PodMemoryHigh
        expr: memory_usage > 0.9
      - alert: ServiceLatencyHigh
        expr: latency > 500
""", encoding="utf-8")

    mock_resp_reload = MagicMock(status_code=200)
    mock_resp_rules = MagicMock(
        status_code=200,
        json=lambda: {"data": {"groups": [{"name": "test_group", "rules": [{"name": "r1"}, {"name": "r2"}]}]}}
    )

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_resp_reload
        mock_client.get.return_value = mock_resp_rules
        mock_client_cls.return_value = mock_client

        result = runner.invoke(app, [
            "seed", "alert-rules",
            "--rules-file", str(rules_file),
            "--prometheus-url", "http://localhost:9090",
        ])
        assert result.exit_code == 0
        assert "Prometheus configuration reload triggered" in result.output
        assert "Alert rules validated successfully (2 rules)" in result.output
        mock_client.post.assert_called_with("http://localhost:9090/-/reload")


def test_cli_seed_alert_rules_missing_file(tmp_path):
    non_existent = tmp_path / "does_not_exist.yaml"
    result = runner.invoke(app, ["seed", "alert-rules", "--rules-file", str(non_existent)])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_cli_seed_alert_rules_invalid_yaml(tmp_path):
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("just_a_string: [unclosed list", encoding="utf-8")
    result = runner.invoke(app, ["seed", "alert-rules", "--rules-file", str(bad_file)])
    assert result.exit_code == 1
    assert "Error parsing alerting rules YAML" in result.output


# -----------------------------------------------------------------------------
# aegis seed mcp-config tests
# -----------------------------------------------------------------------------
def test_cli_seed_mcp_config():
    result = runner.invoke(app, ["seed", "mcp-config"])
    assert result.exit_code == 0
    assert "Configured MCP Servers & Tools" in result.output
    assert "Registered" in result.output


# -----------------------------------------------------------------------------
# aegis verify tests
# -----------------------------------------------------------------------------
def test_cli_verify_all_green():
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    mock_resp_qdrant = MagicMock(status_code=200, json=lambda: {"result": {"collections": [{"name": "aegis_runbooks"}]}})
    mock_resp_ollama = MagicMock(status_code=200, json=lambda: {"models": [{"name": "llama3.1:8b"}]})
    mock_resp_mcp = MagicMock(status_code=200)

    def mock_get(url):
        if "collections" in url or "healthz" in url:
            return mock_resp_qdrant
        if "api/tags" in url:
            return mock_resp_ollama
        return mock_resp_mcp

    with patch("redis.from_url", return_value=mock_redis), \
         patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.side_effect = mock_get
        mock_client_cls.return_value = mock_client

        result = runner.invoke(app, ["verify"])
        assert result.exit_code == 0
        assert "All Aegis services verified and ready to respond!" in result.output


def test_cli_verify_redis_failure():
    with patch("redis.from_url", side_effect=redis.ConnectionError("Redis down")), \
         patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: {})
        mock_client_cls.return_value = mock_client

        result = runner.invoke(app, ["verify"])
        assert result.exit_code == 1
        assert "One or more Aegis components failed health verification" in result.output
