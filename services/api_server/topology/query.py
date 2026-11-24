from collections import deque
from typing import Dict, List, Optional, Set, FrozenSet

from shared.models.topology import TopologyNode, TopologyEdge, SubGraph
from services.api_server.topology.store import TopologyStore


def get_node(store: TopologyStore, node_id: str) -> Optional[TopologyNode]:
    """Retrieve a single node by its ID, or search by name if namespace omitted."""
    node = store.get_node(node_id)
    if node:
        return node
    if "." not in node_id:
        for n in store.all_nodes():
            if n.name == node_id:
                return n
    return None


def get_adjacent_services(
    store: TopologyStore,
    service_id: str,
    max_hops: int = 1,
) -> FrozenSet[str]:
    """
    Returns the set of service IDs connected to service_id within `max_hops`.
    Accepts full node_id (e.g. 'cartservice.boutique') or simple name (e.g. 'cartservice').
    """
    start_id = service_id if store.get_adjacency(service_id) else None
    if not start_id:
        target_node = get_node(store, service_id)
        if not target_node:
            return frozenset()
        start_id = target_node.id

    if max_hops <= 1:
        return frozenset(store.get_adjacency(start_id))

    visited: Set[str] = {start_id}
    reachable: Set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])

    while queue:
        curr_id, dist = queue.popleft()
        if dist >= max_hops:
            continue
        for neighbor_id in store.get_adjacency(curr_id):
            if neighbor_id not in visited:
                visited.add(neighbor_id)
                reachable.add(neighbor_id)
                queue.append((neighbor_id, dist + 1))

    return frozenset(reachable)


def get_subgraph(store: TopologyStore, service_id: str, depth: int = 1) -> SubGraph:
    """
    Extract a subgraph around service_id via BFS up to `depth` hops.
    Nodes within depth are full nodes in `neighbors`.
    Nodes at distance `depth + 1` are included in `stubs` with stub=True.
    """
    target_node = get_node(store, service_id)
    if not target_node:
        # If unknown, return empty subgraph with stub target and mark status=MISSING_DATA
        ns = "default" if "." not in service_id else service_id.split(".")[-1]
        name = service_id.split(".")[0]
        stub_target = TopologyNode(id=service_id, name=name, namespace=ns, stub=True)
        return SubGraph(
            target_node=stub_target,
            neighbors=[],
            edges=[],
            stubs=[],
            status="MISSING_DATA",
            warning=f"Topology subgraph for service '{service_id}' was not found in TopologyStore.",
        )

    visited_dist: Dict[str, int] = {target_node.id: 0}
    queue: deque[tuple[str, int]] = deque([(target_node.id, 0)])

    while queue:
        curr_id, dist = queue.popleft()
        if dist < depth:
            for neighbor_id in store.get_adjacency(curr_id):
                if neighbor_id not in visited_dist:
                    visited_dist[neighbor_id] = dist + 1
                    queue.append((neighbor_id, dist + 1))

    neighbors: List[TopologyNode] = []
    stubs: List[TopologyNode] = []
    stub_ids: Set[str] = set()

    for nid, dist in visited_dist.items():
        if dist == 0:
            continue
        node = store.get_node(nid)
        if node:
            node.stub = False
            neighbors.append(node)

    # Collect stubs at distance depth + 1 (adjacent to nodes within radius, but beyond depth)
    all_in_radius = [target_node.id] + [n.id for n in neighbors]
    for in_id in all_in_radius:
        for adj_id in store.get_adjacency(in_id):
            if adj_id not in visited_dist and adj_id not in stub_ids:
                stub_ids.add(adj_id)
                stub_node = store.get_node(adj_id)
                if stub_node:
                    stub_copy = stub_node.model_copy()
                    stub_copy.stub = True
                    stubs.append(stub_copy)
                else:
                    ns = "default" if "." not in adj_id else adj_id.split(".")[-1]
                    stubs.append(TopologyNode(id=adj_id, name=adj_id.split(".")[0], namespace=ns, stub=True))

    all_subgraph_ids = set([target_node.id] + [n.id for n in neighbors] + [s.id for s in stubs])
    edges: List[TopologyEdge] = [
        e for e in store.all_edges()
        if e.from_node in all_subgraph_ids and e.to_node in all_subgraph_ids
    ]

    return SubGraph(
        target_node=target_node,
        neighbors=neighbors,
        edges=edges,
        stubs=stubs,
    )
