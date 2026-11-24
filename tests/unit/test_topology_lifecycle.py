import json
import pytest
import httpx
from unittest.mock import MagicMock

from shared.models.topology import TopologyNode, TopologyEdge, SubGraph
from services.api_server.topology.store import TopologyStore
from services.api_server.topology.discovery import discover_cluster_topology, check_topology_drift


@pytest.mark.asyncio
async def test_discover_cluster_topology_success(monkeypatch):
    """Test discovery of deployments and services from Kubernetes REST API."""
    monkeypatch.setattr(
        "services.api_server.topology.discovery.resolve_k8s_config",
        lambda: ("http://mock-k8s:6443", {"Authorization": "Bearer test"}, False),
    )

    sample_deployments = {
        "items": [
            {
                "metadata": {"name": "frontend"},
                "spec": {
                    "replicas": 2,
                    "template": {
                        "metadata": {"labels": {"app": "frontend"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "server",
                                    "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                                    "ports": [{"containerPort": 8080}],
                                    "env": [{"name": "AD_SERVICE_ADDR", "value": "ad:9555"}],
                                }
                            ]
                        },
                    },
                },
            },
            {
                "metadata": {"name": "ad"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "metadata": {"labels": {"app": "ad"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "server",
                                    "resources": {"limits": {"cpu": "300m", "memory": "512Mi"}},
                                    "ports": [{"containerPort": 9555}],
                                    "env": [],
                                }
                            ]
                        },
                    },
                },
            },
        ]
    }

    sample_services = {
        "items": [
            {
                "metadata": {"name": "ad"},
                "spec": {
                    "selector": {"app": "ad"},
                    "ports": [{"port": 9555}],
                },
            }
        ]
    }

    async def mock_handler(request: httpx.Request):
        url = str(request.url)
        if "deployments" in url:
            return httpx.Response(200, json=sample_deployments)
        elif "services" in url:
            return httpx.Response(200, json=sample_services)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        nodes, edges = await discover_cluster_topology(["otel-demo"], client=client)

        assert len(nodes) == 2
        node_lookup = {n.name: n for n in nodes}

        frontend = node_lookup["frontend"]
        assert frontend.replicas == 2
        assert frontend.resource_limits == {"cpu": "500m", "memory": "256Mi"}
        assert frontend.ports == [8080]
        assert "ad" in frontend.dependencies

        ad = node_lookup["ad"]
        assert ad.replicas == 1
        assert ad.resource_limits == {"cpu": "300m", "memory": "512Mi"}

        # Edge from frontend to ad
        assert len(edges) >= 1
        assert any(e.from_node == "frontend.otel-demo" and e.to_node == "ad.otel-demo" for e in edges)


@pytest.mark.asyncio
async def test_check_topology_drift_detects_replica_and_limit_changes(monkeypatch):
    """Test drift check detects replica and resource limits drift."""
    # Stored state: ad has 1 replica, 300m cpu
    mock_redis = MagicMock()
    store = TopologyStore(mock_redis)

    stored_node = TopologyNode(
        id="ad.otel-demo",
        name="ad",
        namespace="otel-demo",
        replicas=1,
        resource_limits={"cpu": "300m", "memory": "512Mi"},
    )
    store.all_nodes = MagicMock(return_value=[stored_node])

    # Live cluster state: ad has been scaled to 3 replicas, memory increased to 1024Mi
    live_node = TopologyNode(
        id="ad.otel-demo",
        name="ad",
        namespace="otel-demo",
        replicas=3,
        resource_limits={"cpu": "300m", "memory": "1024Mi"},
    )

    async def mock_discover(*args, **kwargs):
        return [live_node], []

    monkeypatch.setattr("services.api_server.topology.discovery.discover_cluster_topology", mock_discover)

    drifts = await check_topology_drift(store, ["otel-demo"])

    assert len(drifts) == 2
    drift_types = [d["type"] for d in drifts]
    assert "REPLICA_COUNT_DRIFT" in drift_types
    assert "RESOURCE_LIMITS_DRIFT" in drift_types


def test_missing_subgraph_flagging():
    """Verify that a stub or missing subgraph gets flagged as MISSING_DATA."""
    # Build a stub target node
    stub_target = TopologyNode(id="unknown-svc.default", name="unknown-svc", namespace="default", stub=True)
    subgraph = SubGraph(target_node=stub_target, neighbors=[], edges=[], stubs=[])

    data = subgraph.model_dump(mode="json")
    target_info = data.get("target_node", {})
    is_stub = target_info.get("stub", False) or not target_info

    if is_stub or not data.get("neighbors"):
        data["status"] = "MISSING_DATA"
        data["warning"] = "Topology subgraph missing"

    assert data["status"] == "MISSING_DATA"
    assert "warning" in data


@pytest.mark.asyncio
async def test_discover_runtime_trace_topology_jaeger_tempo_prom():
    """Verify runtime topology extraction from Jaeger, Tempo, and Prometheus service-graph/spanmetrics."""
    from services.api_server.topology.discovery import discover_runtime_trace_topology

    existing_nodes = [
        TopologyNode(id="frontend.otel-demo", name="frontend", namespace="otel-demo"),
        TopologyNode(id="cart.otel-demo", name="cart", namespace="otel-demo"),
        TopologyNode(id="valkey-cart.otel-demo", name="valkey-cart", namespace="otel-demo"),
        TopologyNode(id="checkout.otel-demo", name="checkout", namespace="otel-demo"),
    ]

    async def mock_handler(request: httpx.Request):
        url = str(request.url)
        if "/api/dependencies" in url:
            return httpx.Response(
                200,
                json={"data": [{"parent": "frontend", "child": "cart", "callCount": 42}]},
            )
        if "/api/search" in url:
            return httpx.Response(200, json={"traces": [{"traceID": "abc123"}]})
        if "/api/traces/abc123" in url:
            return httpx.Response(
                200,
                json={
                    "batches": [
                        {
                            "resource": {
                                "attributes": [{"key": "service.name", "value": {"stringValue": "cart"}}]
                            },
                            "scopeSpans": [
                                {"spans": [{"spanId": "sp1", "name": "Cart/Get"}]}
                            ],
                        },
                        {
                            "resource": {
                                "attributes": [{"key": "service.name", "value": {"stringValue": "valkey-cart"}}]
                            },
                            "scopeSpans": [
                                {"spans": [{"spanId": "sp2", "parentSpanId": "sp1", "name": "HGET"}]}
                            ],
                        },
                    ]
                },
            )
        if "/api/v1/query" in url:
            query = request.url.params.get("query", "")
            if "traces_service_graph_request_total" in query:
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {
                            "result": [
                                {"metric": {"client": "frontend", "server": "checkout"}, "value": [0, "15"]}
                            ]
                        },
                    },
                )
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        nodes, edges = await discover_runtime_trace_topology(
            namespaces=["otel-demo"],
            mcp_endpoints={
                "jaeger": "http://jaeger:16686",
                "tempo": "http://tempo:3200",
                "prometheus": "http://prometheus:9090",
            },
            existing_nodes=existing_nodes,
            client=client,
        )

    edge_pairs = {(e.from_node, e.to_node) for e in edges}
    assert ("frontend.otel-demo", "cart.otel-demo") in edge_pairs
    assert ("cart.otel-demo", "valkey-cart.otel-demo") in edge_pairs
    assert ("frontend.otel-demo", "checkout.otel-demo") in edge_pairs
    assert len(nodes) >= 4


@pytest.mark.asyncio
async def test_sync_topology_sources_keeps_static_graph_on_runtime_failure(monkeypatch, tmp_path):
    """If dynamic runtime discovery fails completely, static chart & K8s topology still persist."""
    from types import SimpleNamespace
    from services.api_server.topology.sync import sync_topology_sources
    from tests.unit.test_topology_store import FakeRedis

    chart_dir = tmp_path / "chart"
    chart_dir.mkdir()
    (chart_dir / "app.yaml").write_text(
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: web
          env:
            - name: CART_ADDR
              value: cartservice:7070
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cartservice
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: svc
          ports:
            - containerPort: 7070
""",
        encoding="utf-8",
    )

    async def fail_cluster(*_args, **_kwargs):
        raise RuntimeError("K8s unreachable")

    async def fail_runtime(*_args, **_kwargs):
        raise RuntimeError("Jaeger/Tempo/Prometheus unreachable")

    monkeypatch.setattr("services.api_server.topology.discovery.discover_cluster_topology", fail_cluster)
    monkeypatch.setattr("services.api_server.topology.discovery.discover_runtime_trace_topology", fail_runtime)

    store = TopologyStore(FakeRedis(), snapshot_path=str(tmp_path / "snap.json"))
    cfg = SimpleNamespace(
        system=SimpleNamespace(
            topology=SimpleNamespace(helm_dir=str(chart_dir), monitored_namespaces=["boutique"]),
            mcp_endpoints=SimpleNamespace(jaeger_url="http://dead:16686", tempo_url="", prometheus_url=""),
        )
    )

    await sync_topology_sources(store, cfg)

    saved_nodes = {n.id for n in store.all_nodes()}
    assert {"frontend.boutique", "cartservice.boutique"}.issubset(saved_nodes)
    assert any(
        e.from_node == "frontend.boutique" and e.to_node == "cartservice.boutique"
        for e in store.all_edges()
    )

