import pytest
from starlette.testclient import TestClient
from shared.models.topology import TopologyNode, TopologyEdge
from services.api_server.topology.store import TopologyStore
from services.mcp.topology.server import mcp, topology_query_node
from tests.unit.test_correlator import CorrelatorFakeRedis


@pytest.fixture
def fake_redis():
    return CorrelatorFakeRedis()


@pytest.fixture
def topology_store(fake_redis, tmp_path):
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "snap.json"))
    nodes = [
        TopologyNode(
            id="redis-cart.boutique",
            name="redis-cart",
            namespace="boutique",
            ports=[6379],
            resource_limits={"cpu": "100m", "memory": "128Mi"},
            dependencies=[],
        ),
        TopologyNode(
            id="cartservice.boutique",
            name="cartservice",
            namespace="boutique",
            ports=[7070],
            dependencies=["redis-cart.boutique"],
        ),
    ]
    edges = [
        TopologyEdge(from_node="cartservice.boutique", to_node="redis-cart.boutique", port=6379),
    ]
    store.save(nodes, edges)
    return store


@pytest.mark.asyncio
async def test_topology_query_node_returns_detail(fake_redis, topology_store):
    res = await topology_query_node(
        node_id="redis-cart.boutique",
        incident_id="inc-test-1",
        redis_client=fake_redis,
        store=topology_store,
        max_budget=5,
    )
    assert res["id"] == "redis-cart.boutique"
    assert res["name"] == "redis-cart"
    assert res["ports"] == [6379]
    assert res["stub"] is False
    assert res["resource_limits"] == {"cpu": "100m", "memory": "128Mi"}


@pytest.mark.asyncio
async def test_topology_query_node_budget_exhausted_returns_error(fake_redis, topology_store):
    # Perform 5 valid calls within budget
    for i in range(5):
        res = await topology_query_node(
            node_id="redis-cart.boutique",
            incident_id="inc-budget-1",
            redis_client=fake_redis,
            store=topology_store,
            max_budget=5,
        )
        assert res["id"] == "redis-cart.boutique"

    # 6th call must raise ValueError
    with pytest.raises(ValueError) as exc:
        await topology_query_node(
            node_id="redis-cart.boutique",
            incident_id="inc-budget-1",
            redis_client=fake_redis,
            store=topology_store,
            max_budget=5,
        )
    assert "Query budget exhausted" in str(exc.value)


@pytest.mark.asyncio
async def test_topology_query_node_unknown_returns_stub(fake_redis, topology_store):
    res = await topology_query_node(
        node_id="unknownservice.boutique",
        incident_id="inc-unknown-1",
        redis_client=fake_redis,
        store=topology_store,
        max_budget=5,
    )
    assert res["id"] == "unknownservice.boutique"
    assert res["name"] == "unknownservice"
    assert res["namespace"] == "boutique"
    assert res["stub"] is True


def test_topology_mcp_health_check_healthy(mocker):
    fake_r = mocker.MagicMock()
    fake_r.ping.return_value = True
    mocker.patch("redis.from_url", return_value=fake_r)

    client = TestClient(mcp.streamable_http_app())
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["redis"] == "ok"
    assert data["service"] == "topology-mcp"


def test_topology_mcp_health_check_unhealthy(mocker):
    fake_r = mocker.MagicMock()
    fake_r.ping.side_effect = ConnectionError("Redis down")
    mocker.patch("redis.from_url", return_value=fake_r)

    client = TestClient(mcp.streamable_http_app())
    res = client.get("/health")
    assert res.status_code == 503
    data = res.json()
    assert data["status"] == "degraded"
    assert "unreachable" in data["redis"]
    assert data["service"] == "topology-mcp"
