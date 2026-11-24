import pytest
from shared.models.topology import TopologyNode, TopologyEdge
from services.api_server.topology.store import TopologyStore
from services.api_server.topology.query import get_node, get_subgraph, get_adjacent_services
from tests.unit.test_topology_store import FakeRedis


@pytest.fixture
def populated_store(tmp_path):
    store = TopologyStore(FakeRedis(), snapshot_path=str(tmp_path / "snap.json"))
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
            id="frontend.boutique",
            name="frontend",
            namespace="boutique",
            ports=[8080],
            dependencies=["cartservice.boutique", "productcatalogservice.boutique"],
        ),
        TopologyNode(
            id="redis-cart.boutique",
            name="redis-cart",
            namespace="boutique",
            ports=[6379],
            dependencies=[],
        ),
        TopologyNode(
            id="productcatalogservice.boutique",
            name="productcatalogservice",
            namespace="boutique",
            ports=[3550],
            dependencies=[],
        ),
    ]
    edges = [
        TopologyEdge(from_node="frontend.boutique", to_node="cartservice.boutique", port=7070),
        TopologyEdge(from_node="frontend.boutique", to_node="productcatalogservice.boutique", port=3550),
        TopologyEdge(from_node="cartservice.boutique", to_node="redis-cart.boutique", port=6379),
    ]
    store.save(nodes, edges)
    return store


def test_get_subgraph_depth1_returns_correct_nodes(populated_store):
    # cartservice is adjacent to frontend and redis-cart
    # frontend is adjacent to productcatalogservice (which is at depth 2 from cartservice)
    subgraph = get_subgraph(populated_store, "cartservice.boutique", depth=1)

    assert subgraph.target_node.id == "cartservice.boutique"
    assert not subgraph.target_node.stub

    neighbor_ids = [n.id for n in subgraph.neighbors]
    assert "frontend.boutique" in neighbor_ids
    assert "redis-cart.boutique" in neighbor_ids
    assert "productcatalogservice.boutique" not in neighbor_ids

    # productcatalogservice is at depth 2, so it should be in stubs!
    stub_ids = [s.id for s in subgraph.stubs]
    assert "productcatalogservice.boutique" in stub_ids
    assert any(s.id == "productcatalogservice.boutique" and s.stub is True for s in subgraph.stubs)


def test_get_subgraph_marks_far_nodes_as_stubs(populated_store):
    subgraph = get_subgraph(populated_store, "redis-cart.boutique", depth=1)
    assert subgraph.target_node.id == "redis-cart.boutique"
    neighbor_ids = [n.id for n in subgraph.neighbors]
    assert neighbor_ids == ["cartservice.boutique"]

    stub_ids = [s.id for s in subgraph.stubs]
    # frontend is neighbor of cartservice, so distance 2 -> stub
    assert "frontend.boutique" in stub_ids


def test_get_node_returns_none_for_unknown_id(populated_store):
    node = get_node(populated_store, "unknown.service")
    assert node is None


def test_get_node_by_short_name(populated_store):
    node = get_node(populated_store, "cartservice")
    assert node is not None
    assert node.id == "cartservice.boutique"


def test_get_adjacent_services(populated_store):
    adj_full = get_adjacent_services(populated_store, "cartservice.boutique")
    assert "frontend.boutique" in adj_full
    assert "redis-cart.boutique" in adj_full

    adj_short = get_adjacent_services(populated_store, "cartservice")
    assert "frontend.boutique" in adj_short
    assert "redis-cart.boutique" in adj_short


def test_get_subgraph_unknown_target(populated_store):
    subgraph = get_subgraph(populated_store, "missing.boutique", depth=1)
    assert subgraph.target_node.id == "missing.boutique"
    assert subgraph.target_node.stub is True
    assert subgraph.neighbors == []
    assert subgraph.stubs == []


def test_get_adjacent_services_multihop(populated_store):
    adj_1 = get_adjacent_services(populated_store, "frontend.boutique", max_hops=1)
    assert "cartservice.boutique" in adj_1
    assert "productcatalogservice.boutique" in adj_1
    assert "redis-cart.boutique" not in adj_1

    adj_2 = get_adjacent_services(populated_store, "frontend.boutique", max_hops=2)
    assert "cartservice.boutique" in adj_2
    assert "redis-cart.boutique" in adj_2
    assert "productcatalogservice.boutique" in adj_2
