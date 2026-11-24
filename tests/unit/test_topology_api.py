import pytest
from fastapi.testclient import TestClient
from shared.models.topology import TopologyNode, TopologyEdge
from services.api_server.main import create_app
from services.api_server.topology.store import TopologyStore
from tests.unit.test_correlator import CorrelatorFakeRedis


@pytest.fixture
def api_client(mocker, tmp_path):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    # Seed topology data in app's store
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "snap.json"))
    nodes = [
        TopologyNode(
            id="cartservice.boutique",
            name="cartservice",
            namespace="boutique",
            ports=[7070],
            resource_limits={"cpu": "300m", "memory": "256Mi"},
            dependencies=["redis-cart.boutique"],
        ),
        TopologyNode(
            id="redis-cart.boutique",
            name="redis-cart",
            namespace="boutique",
            ports=[6379],
            dependencies=[],
        ),
    ]
    edges = [
        TopologyEdge(from_node="cartservice.boutique", to_node="redis-cart.boutique", port=6379),
    ]
    store.save(nodes, edges)

    app.state.topology_store = store
    app.state.redis = fake_redis

    headers = {"X-API-Key": app.state.config.system.system.api_key}

    with TestClient(app) as client:
        client.headers.update(headers)
        yield client


def test_api_get_topology_returns_nodes_and_edges(api_client):
    res = api_client.get("/v1/topology")
    assert res.status_code == 200
    data = res.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1
    assert data["nodes"][0]["id"] == "cartservice.boutique"


def test_api_get_topology_node_found(api_client):
    res = api_client.get("/v1/topology/node/cartservice.boutique")
    assert res.status_code == 200
    node = res.json()
    assert node["id"] == "cartservice.boutique"
    assert node["resource_limits"] == {"cpu": "300m", "memory": "256Mi"}
    assert node["ports"] == [7070]


def test_api_get_topology_node_not_found_404(api_client):
    res = api_client.get("/v1/topology/node/nonexistent.boutique")
    assert res.status_code == 404


def test_api_refresh_topology(api_client, mocker):
    mocker.patch(
        "services.api_server.routers.topology.parse_helm_dir",
        return_value=(
            [TopologyNode(id="frontend.boutique", name="frontend", namespace="boutique")],
            [],
        ),
    )
    res = api_client.post("/v1/topology/refresh")
    assert res.status_code == 200
    assert res.json()["status"] == "refreshed"
    assert res.json()["node_count"] == 1
