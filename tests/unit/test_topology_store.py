import json
import pytest
from shared.models.topology import TopologyNode, TopologyEdge
from services.api_server.topology.store import TopologyStore


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def scan_iter(self, match):
        prefix = match.replace("*", "")
        for k in self.store:
            if k.startswith(prefix):
                yield k


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def sample_data():
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
            dependencies=["cartservice.boutique"],
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
        TopologyEdge(from_node="frontend.boutique", to_node="cartservice.boutique", port=7070),
        TopologyEdge(from_node="cartservice.boutique", to_node="redis-cart.boutique", port=6379),
    ]
    return nodes, edges


def test_store_save_and_retrieve_node(fake_redis, sample_data, tmp_path):
    nodes, edges = sample_data
    snapshot_file = str(tmp_path / "snapshot.json")
    store = TopologyStore(fake_redis, snapshot_path=snapshot_file)

    store.save(nodes, edges)

    node = store.get_node("cartservice.boutique")
    assert node is not None
    assert node.id == "cartservice.boutique"
    assert node.name == "cartservice"
    assert node.ports == [7070]
    assert node.resource_limits == {"cpu": "300m", "memory": "256Mi"}


def test_store_get_adjacency_returns_neighbors(fake_redis, sample_data, tmp_path):
    nodes, edges = sample_data
    snapshot_file = str(tmp_path / "snapshot.json")
    store = TopologyStore(fake_redis, snapshot_path=snapshot_file)

    store.save(nodes, edges)

    cart_neighbors = store.get_adjacency("cartservice.boutique")
    assert "frontend.boutique" in cart_neighbors
    assert "redis-cart.boutique" in cart_neighbors

    frontend_neighbors = store.get_adjacency("frontend.boutique")
    assert "cartservice.boutique" in frontend_neighbors


def test_store_snapshot_writes_json_file(fake_redis, sample_data, tmp_path):
    nodes, edges = sample_data
    snapshot_file = str(tmp_path / "snapshot.json")
    store = TopologyStore(fake_redis, snapshot_path=snapshot_file)

    store.save(nodes, edges)

    assert (tmp_path / "snapshot.json").exists()
    with open(snapshot_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "nodes" in data
    assert len(data["nodes"]) == 3
    assert "edges" in data
    assert len(data["edges"]) == 2
    assert "graph" in data
    assert "cartservice.boutique" in data["graph"]


def test_store_get_unknown_node_returns_none(fake_redis, tmp_path):
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "snap.json"))
    assert store.get_node("nonexistent.default") is None
    assert store.get_adjacency("nonexistent.default") == []
