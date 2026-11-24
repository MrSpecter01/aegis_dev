import json
import httpx
import pytest
from unittest.mock import AsyncMock
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.kubernetes_write import (
    restart_pod,
    scale_deployment,
    rollback_release,
    update_resource_limits,
    drain_node,
    register_write_tools,
)


@pytest.fixture
def mock_approved_redis():
    mock = AsyncMock()
    mock.get.return_value = json.dumps({"status": "approved", "action_id": "act-test"})
    return mock


@pytest.fixture
def mock_unapproved_redis():
    mock = AsyncMock()
    mock.get.return_value = json.dumps({"status": "pending", "action_id": "act-test"})
    return mock


@pytest.mark.asyncio
async def test_restart_pod_valid_approved(mock_approved_redis):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert "/api/v1/namespaces/boutique/pods/cartservice-123" in str(request.url)
        return httpx.Response(200, json={"status": "Success"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await restart_pod(
            namespace="boutique",
            pod_name="cartservice-123",
            incident_id="inc-1",
            action_id="act-test",
            client=client,
            redis_client=mock_approved_redis,
            base_url="http://k8s.test",
        )
        assert res["success"] is True
        assert "deleted successfully" in res["message"]


@pytest.mark.asyncio
async def test_restart_pod_not_approved_rejected(mock_unapproved_redis):
    res = await restart_pod(
        namespace="boutique",
        pod_name="cartservice-123",
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_unapproved_redis,
        base_url="http://k8s.test",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "not approved" in res["error"]


@pytest.mark.asyncio
async def test_restart_pod_injection_rejected(mock_approved_redis):
    res = await restart_pod(
        namespace="boutique",
        pod_name="cartservice; rm -rf /",
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "Prompt injection" in res["error"]


@pytest.mark.asyncio
async def test_restart_pod_invalid_name(mock_approved_redis):
    res = await restart_pod(
        namespace="boutique",
        pod_name="Invalid_Pod_Name!",
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "Must be a valid Kubernetes DNS" in res["error"]


@pytest.mark.asyncio
async def test_scale_deployment_valid(mock_approved_redis):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert "/namespaces/boutique/deployments/frontend/scale" in str(request.url)
        body = json.loads(request.content)
        assert body["spec"]["replicas"] == 5
        return httpx.Response(200, json={"status": "Success"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await scale_deployment(
            namespace="boutique",
            deployment="frontend",
            replicas=5,
            incident_id="inc-1",
            action_id="act-test",
            client=client,
            redis_client=mock_approved_redis,
            base_url="http://k8s.test",
        )
        assert res["success"] is True
        assert res["replicas"] == 5


@pytest.mark.asyncio
async def test_scale_deployment_replicas_out_of_bounds(mock_approved_redis):
    # Too high (> 20)
    res_high = await scale_deployment(
        namespace="boutique",
        deployment="frontend",
        replicas=99,
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res_high["success"] is False
    assert res_high["isError"] is True
    assert "exceeds maximum configured limit" in res_high["error"]

    # Too low (< 1)
    res_zero = await scale_deployment(
        namespace="boutique",
        deployment="frontend",
        replicas=0,
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res_zero["success"] is False
    assert res_zero["isError"] is True
    assert "at least 1" in res_zero["error"]


@pytest.mark.asyncio
async def test_rollback_release_valid(mock_approved_redis, monkeypatch):
    monkeypatch.setenv("ARGOCD_URL", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        return httpx.Response(200, json={"metadata": {"name": "cartservice"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await rollback_release(
            namespace="boutique",
            app="cartservice",
            revision=2,
            incident_id="inc-1",
            action_id="act-test",
            client=client,
            redis_client=mock_approved_redis,
            base_url="http://k8s.test",
        )
        assert res["success"] is True
        assert res["revision"] == 2


@pytest.mark.asyncio
async def test_rollback_release_invalid_revision(mock_approved_redis):
    res = await rollback_release(
        namespace="boutique",
        app="cartservice",
        revision=-1,
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "must be an integer >= 0" in res["error"]


@pytest.mark.asyncio
async def test_update_resource_limits_valid(mock_approved_redis):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"spec": {"template": {"spec": {"containers": [{"name": "frontend"}]}}}})
        assert request.method == "PATCH"
        body = json.loads(request.content)
        assert body["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "500m"
        return httpx.Response(200, json={"metadata": {"name": "frontend"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await update_resource_limits(
            namespace="boutique",
            deployment="frontend",
            limits={"cpu": "500m", "memory": "512Mi"},
            incident_id="inc-1",
            action_id="act-test",
            client=client,
            redis_client=mock_approved_redis,
            base_url="http://k8s.test",
        )
        assert res["success"] is True
        assert res["limits"]["cpu"] == "500m"


@pytest.mark.asyncio
async def test_update_resource_limits_bad_format(mock_approved_redis):
    res = await update_resource_limits(
        namespace="boutique",
        deployment="frontend",
        limits={"cpu": "not-valid-cpu"},
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "Invalid cpu format" in res["error"]


@pytest.mark.asyncio
async def test_drain_node_valid(mock_approved_redis):
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if request.method == "PATCH" and "/nodes/node-1" in url_str:
            return httpx.Response(200, json={"metadata": {"name": "node-1"}})
        elif request.method == "GET" and "/pods" in url_str:
            return httpx.Response(200, json={"items": [
                {"metadata": {"name": "pod-1", "namespace": "boutique"}},
                {"metadata": {"name": "pod-2", "namespace": "boutique"}},
            ]})
        elif request.method == "POST" and "/eviction" in url_str:
            return httpx.Response(201, json={"status": "Success"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await drain_node(
            node="node-1",
            grace_period_seconds=120,
            incident_id="inc-1",
            action_id="act-test",
            client=client,
            redis_client=mock_approved_redis,
            base_url="http://k8s.test",
        )
        assert res["success"] is True
        assert res["cordoned"] is True
        assert res["evicted_count"] == 2


@pytest.mark.asyncio
async def test_drain_node_grace_out_of_bounds(mock_approved_redis):
    # Too short (< 60s)
    res_short = await drain_node(
        node="node-1",
        grace_period_seconds=10,
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res_short["success"] is False
    assert res_short["isError"] is True
    assert "between 60 and 600 seconds" in res_short["error"]

    # Too long (> 600s)
    res_long = await drain_node(
        node="node-1",
        grace_period_seconds=3600,
        incident_id="inc-1",
        action_id="act-test",
        redis_client=mock_approved_redis,
        base_url="http://k8s.test",
    )
    assert res_long["success"] is False
    assert res_long["isError"] is True
    assert "between 60 and 600 seconds" in res_long["error"]


def test_kubernetes_write_tool_registration():
    app = FastMCP("test-write-mcp")
    register_write_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    expected = [
        "kubernetes_restart_pod",
        "kubernetes_scale_deployment",
        "kubernetes_rollback_release",
        "kubernetes_update_resource_limits",
        "kubernetes_drain_node",
    ]
    for exp in expected:
        assert exp in tools
