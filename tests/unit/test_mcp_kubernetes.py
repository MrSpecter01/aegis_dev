import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.kubernetes import (
    k8s_get_events,
    k8s_get_pod_status,
    k8s_get_pod_diagnostics,
    k8s_get_node_metrics,
    k8s_get_configmap_diff,
    validate_k8s_name,
    validate_label_selector,
    register_tools,
)


@pytest.mark.asyncio
async def test_k8s_get_events_success():
    mock_resp = {
        "items": [
            {
                "reason": "OOMKilled",
                "message": "Pod cartservice memory exceeded limits",
                "type": "Warning",
                "count": 3,
                "firstTimestamp": "2026-09-21T12:00:00Z",
                "lastTimestamp": "2026-09-21T12:05:00Z",
                "involvedObject": {"name": "cartservice-abc", "kind": "Pod"},
            },
            {
                "reason": "Scheduled",
                "message": "Successfully assigned to node-1",
                "type": "Normal",
                "count": 1,
                "involvedObject": {"name": "frontend-xyz", "kind": "Pod"},
            },
        ]
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_resp
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    # Test with resource filter
    res = await k8s_get_events(
        namespace="boutique",
        resource="cartservice-abc",
        client=mock_client,
        base_url="http://mock-k8s:8001",
    )

    assert res["namespace"] == "boutique"
    assert res["resource"] == "cartservice-abc"
    assert res["event_count"] == 1
    assert res["events"][0]["reason"] == "OOMKilled"
    assert res["events"][0]["count"] == 3


@pytest.mark.asyncio
async def test_k8s_get_pod_status_success():
    mock_resp = {
        "items": [
            {
                "metadata": {"name": "cartservice-abc123"},
                "spec": {"nodeName": "worker-node-1"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "server",
                            "ready": True,
                            "restartCount": 2,
                            "state": {"running": {"startedAt": "2026-09-21T12:00:00Z"}},
                        }
                    ],
                },
            }
        ]
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_resp
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await k8s_get_pod_status(
        namespace="boutique",
        label_selector="app=cartservice",
        client=mock_client,
        base_url="http://mock-k8s:8001",
    )

    assert res["namespace"] == "boutique"
    assert res["pod_count"] == 1
    pod = res["pods"][0]
    assert pod["name"] == "cartservice-abc123"
    assert pod["phase"] == "Running"
    assert pod["ready"] is True
    assert pod["restart_count"] == 2
    assert pod["node_name"] == "worker-node-1"


@pytest.mark.asyncio
async def test_k8s_get_node_metrics_success_and_not_found():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = {
        "usage": {"cpu": "250m", "memory": "2048Mi"},
        "timestamp": "2026-09-21T12:10:00Z",
    }
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await k8s_get_node_metrics(
        node="worker-1",
        client=mock_client,
        base_url="http://mock-k8s:8001",
    )
    assert res["available"] is True
    assert res["usage"]["cpu"] == "250m"

    # 404 not found
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404
    mock_client.get.return_value = mock_resp_404

    res_404 = await k8s_get_node_metrics(
        node="unknown-node",
        client=mock_client,
        base_url="http://mock-k8s:8001",
    )
    assert res_404["available"] is False
    assert "metrics-server may not be installed" in res_404["message"]


@pytest.mark.asyncio
async def test_k8s_get_configmap_diff_success():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = {
        "metadata": {
            "name": "app-config",
            "resourceVersion": "12345",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"TIMEOUT":"30s"}}'
            },
        },
        "data": {"TIMEOUT": "30s", "LOG_LEVEL": "DEBUG"},
    }
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await k8s_get_configmap_diff(
        namespace="boutique",
        name="app-config",
        client=mock_client,
        base_url="http://mock-k8s:8001",
    )

    assert res["found"] is True
    assert "TIMEOUT" in res["data_keys"]
    assert res["data"]["TIMEOUT"] == "30s"
    assert res["resource_version"] == "12345"


@pytest.mark.asyncio
async def test_k8s_input_validation():
    with pytest.raises(ValueError, match="cannot be empty"):
        await k8s_get_events(namespace="")

    with pytest.raises(ValueError, match="Invalid namespace"):
        await k8s_get_events(namespace="Namespace_Capital")

    with pytest.raises(ValueError, match="Invalid characters in label_selector"):
        await k8s_get_pod_status(namespace="boutique", label_selector="app; rm -rf")


@pytest.mark.asyncio
async def test_k8s_get_pod_diagnostics_success():
    pod_resp = {
        "items": [
            {
                "metadata": {"name": "cartservice-7d8b9c-xyz", "labels": {"app": "cartservice"}},
                "spec": {"nodeName": "worker-1"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "cartservice",
                            "ready": False,
                            "restartCount": 5,
                            "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "Back-off restarting"}},
                            "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                        }
                    ],
                },
            }
        ]
    }
    event_resp = {
        "items": [
            {
                "type": "Warning",
                "reason": "OOMKilling",
                "message": "Memory limit exceeded",
                "count": 2,
                "lastTimestamp": "2026-10-31T12:00:00Z",
                "involvedObject": {"kind": "Pod", "name": "cartservice-7d8b9c-xyz"},
            }
        ]
    }

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    def mock_get(endpoint, *args, **kwargs):
        mock_obj = MagicMock()
        mock_obj.status_code = 200
        mock_obj.raise_for_status.return_value = None
        if "pods" in endpoint:
            mock_obj.json.return_value = pod_resp
        else:
            mock_obj.json.return_value = event_resp
        return mock_obj

    mock_client.get.side_effect = mock_get

    res = await k8s_get_pod_diagnostics(
        namespace="boutique",
        service_or_pod="cartservice",
        client=mock_client,
        base_url="http://mock-k8s:8001",
    )

    assert res["namespace"] == "boutique"
    assert res["found"] is True
    assert res["pod_count"] == 1
    assert len(res["critical_issues"]) >= 1
    assert any("OOMKilled" in issue for issue in res["critical_issues"])
    assert len(res["recent_events"]) == 1
    assert res["recent_events"][0]["reason"] == "OOMKilling"


def test_k8s_tool_registration():
    app = FastMCP("test-server")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "k8s_get_events" in tools
    assert "k8s_get_pod_status" in tools
    assert "k8s_get_pod_diagnostics" in tools
    assert "k8s_get_node_metrics" in tools
    assert "k8s_get_configmap_diff" in tools
