from datetime import datetime, timezone
import json
import logging
import os
from typing import Dict, List, Optional, Any, Set

from shared.models.topology import TopologyNode, TopologyEdge
from shared.versioning import (
    TOPOLOGY_META_KEY,
    compute_topology_digest,
    read_meta,
    write_meta,
)

logger = logging.getLogger("aegis.topology.store")


class TopologyStore:
    """Store and retrieve topology nodes and graph adjacency in Redis with snapshot persistence."""

    def __init__(self, redis_client: Any, snapshot_path: str = "/data/topology_snapshot.json") -> None:
        self.redis = redis_client
        self.snapshot_path = snapshot_path

    def get_meta(self) -> Dict[str, Any]:
        """Return stored SHA-256 digest and version metadata for the topology snapshot."""
        return read_meta(self.redis, TOPOLOGY_META_KEY)

    def save(
        self,
        nodes: List[TopologyNode],
        edges: List[TopologyEdge],
        *,
        updated_by: str = "system",
        force: bool = False,
    ) -> Dict[str, Any]:
        """Persist all nodes, edges, adjacency graph, and SHA-256 version metadata to Redis and disk."""
        new_sha = compute_topology_digest(nodes, edges)
        old_meta = self.get_meta()
        old_sha = str(old_meta.get("sha256") or "")
        old_version = int(old_meta.get("version") or 0)

        if not force and old_sha and old_sha == new_sha and self.all_nodes():
            return {
                "changed": False,
                "old_sha": old_sha,
                "sha256": new_sha,
                "version": max(1, old_version),
                "node_count": len(nodes),
                "edge_count": len(edges),
            }

        node_ids: List[str] = []
        for node in nodes:
            key = f"topology:node:{node.id}"
            self.redis.set(key, node.model_dump_json())
            node_ids.append(node.id)

        # Store index of node IDs
        self.redis.set("topology:nodes:list", json.dumps(node_ids))

        # Build bidirectional adjacency map
        adjacency: Dict[str, Set[str]] = {n.id: set() for n in nodes}
        for edge in edges:
            if edge.from_node in adjacency:
                adjacency[edge.from_node].add(edge.to_node)
            else:
                adjacency[edge.from_node] = {edge.to_node}

            if edge.to_node in adjacency:
                adjacency[edge.to_node].add(edge.from_node)
            else:
                adjacency[edge.to_node] = {edge.from_node}

        adj_serializable = {k: sorted(list(v)) for k, v in adjacency.items()}
        self.redis.set("topology:graph", json.dumps(adj_serializable))

        # Store edges
        edges_serializable = [e.model_dump(by_alias=True) for e in edges]
        self.redis.set("topology:edges", json.dumps(edges_serializable))

        new_version = old_version + 1
        meta = {
            "version": new_version,
            "sha256": new_sha,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": updated_by,
            "node_count": len(nodes),
            "edge_count": len(edges),
        }
        write_meta(self.redis, TOPOLOGY_META_KEY, meta)

        # Write disk snapshot
        self.snapshot(meta=meta)
        return {
            "changed": True,
            "old_sha": old_sha,
            "sha256": new_sha,
            "version": new_version,
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

    def get_node(self, node_id: str) -> Optional[TopologyNode]:
        """Fetch a single node by its ID (e.g. 'cartservice.boutique')."""
        raw = self.redis.get(f"topology:node:{node_id}")
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return TopologyNode.model_validate_json(raw)
        except Exception as e:
            logger.error(f"Failed to deserialise TopologyNode {node_id}: {e}")
            return None

    def get_adjacency(self, node_id: str) -> List[str]:
        """Return list of adjacent node IDs directly connected to node_id."""
        raw = self.redis.get("topology:graph")
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            graph = json.loads(raw)
            return graph.get(node_id, [])
        except Exception as e:
            logger.error(f"Failed to read topology:graph: {e}")
            return []

    def all_nodes(self) -> List[TopologyNode]:
        """Return all persisted TopologyNodes."""
        raw_list = self.redis.get("topology:nodes:list")
        node_ids: List[str] = []
        if raw_list:
            if isinstance(raw_list, bytes):
                raw_list = raw_list.decode("utf-8")
            try:
                node_ids = json.loads(raw_list)
            except Exception:
                node_ids = []

        nodes: List[TopologyNode] = []
        if node_ids:
            for nid in node_ids:
                node = self.get_node(nid)
                if node:
                    nodes.append(node)
        else:
            # Fallback scan
            try:
                for key in self.redis.scan_iter("topology:node:*"):
                    if isinstance(key, bytes):
                        key = key.decode("utf-8")
                    nid = key.replace("topology:node:", "")
                    node = self.get_node(nid)
                    if node:
                        nodes.append(node)
            except Exception as exc:
                logger.warning("Failed during topology:node scan: %s", exc)

        return sorted(nodes, key=lambda n: n.id)

    def all_edges(self) -> List[TopologyEdge]:
        """Return all persisted TopologyEdges."""
        raw = self.redis.get("topology:edges")
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
            return [TopologyEdge.model_validate(e) for e in data]
        except Exception as e:
            logger.error(f"Failed to read topology:edges: {e}")
            return []

    def snapshot(self, meta: Optional[Dict[str, Any]] = None) -> None:
        """Write current topology graph and digest metadata to snapshot_path as JSON."""
        if not self.snapshot_path:
            return

        parent_dir = os.path.dirname(self.snapshot_path)
        if parent_dir:
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as e:
                logger.warning(f"Could not create parent directory {parent_dir}: {e}")

        graph_raw = self.redis.get("topology:graph")
        graph_dict = {}
        if graph_raw:
            if isinstance(graph_raw, bytes):
                graph_raw = graph_raw.decode("utf-8")
            try:
                graph_dict = json.loads(graph_raw)
            except Exception as exc:
                logger.warning("Failed to decode topology:graph JSON: %s", exc)

        nodes_list = self.all_nodes()
        edges_list = self.all_edges()
        eff_meta = meta or self.get_meta()
        sha_val = eff_meta.get("sha256") or compute_topology_digest(nodes_list, edges_list)

        data = {
            "version": int(eff_meta.get("version") or 1),
            "sha256": sha_val,
            "updated_at": eff_meta.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            "updated_by": eff_meta.get("updated_by") or "system",
            "nodes": [n.model_dump() for n in nodes_list],
            "edges": [e.model_dump(by_alias=True) for e in edges_list],
            "graph": graph_dict,
        }

        try:
            with open(self.snapshot_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write topology snapshot to {self.snapshot_path}: {e}")
