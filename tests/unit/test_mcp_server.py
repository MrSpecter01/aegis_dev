import pytest
from starlette.testclient import TestClient

from services.mcp.server import create_unified_mcp
from services.mcp.standalone import create_standalone_mcp, get_domain_port


def test_unified_server_registers_all_tools():
    app = create_unified_mcp(host="127.0.0.1", port=9000)
    tools = {t.name for t in app._tool_manager.list_tools()}

    expected_tools = {
        "prometheus_query",
        "loki_query",
        "tempo_search",
        "tempo_get_trace",
        "k8s_get_events",
        "k8s_get_pod_status",
        "k8s_get_node_metrics",
        "k8s_get_configmap_diff",
        "argocd_get_app_status",
        "argocd_get_sync_history",
        "github_get_commits",
        "topology_query_node",
    }

    assert expected_tools.issubset(tools)
    assert len(tools) >= len(expected_tools)


def test_unified_server_health_check():
    app = create_unified_mcp(host="127.0.0.1", port=9000)
    client = TestClient(app.streamable_http_app())
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "aegis-mcp"
    assert data["tool_count"] >= 12
    assert "prometheus" in data["domains"]


def test_standalone_runner_prometheus():
    app = create_standalone_mcp(service="prometheus")
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert tools == ["prometheus_query"]

    client = TestClient(app.streamable_http_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["domain"] == "prometheus"


def test_standalone_runner_loki():
    app = create_standalone_mcp(service="loki")
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert tools == ["loki_query"]


def test_standalone_runner_unknown_service():
    with pytest.raises(ValueError, match="Unknown standalone service"):
        create_standalone_mcp(service="non_existent_service")


def test_get_domain_ports():
    assert get_domain_port("prometheus") == 9001
    assert get_domain_port("loki") == 9002
    assert get_domain_port("tempo") == 9003
    assert get_domain_port("kubernetes") == 9004
    assert get_domain_port("argocd") == 9005
    assert get_domain_port("github") == 9006
    assert get_domain_port("topology") == 9008
