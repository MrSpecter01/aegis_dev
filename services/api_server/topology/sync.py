from datetime import datetime, timezone
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from shared.audit.tracker import record_interaction_async
from shared.models.topology import TopologyNode, TopologyEdge
from shared.versioning import (
    clear_pending_drift,
    compute_topology_digest,
    set_pending_drift,
)
from services.api_server.topology.store import TopologyStore
from services.api_server.topology.parser import parse_helm_dir

logger = logging.getLogger("aegis.topology.sync")


async def build_live_topology(
    config: Any,
    parse_helm_dir_fn: Optional[Callable[..., Any]] = None,
) -> Tuple[List[TopologyNode], List[TopologyEdge]]:
    """Build merged Helm + live Kubernetes + runtime trace topology without modifying TopologyStore."""
    helm_dir = "./charts"
    if hasattr(config, "system") and hasattr(config.system, "topology") and hasattr(config.system.topology, "helm_dir"):
        helm_dir = config.system.topology.helm_dir

    monitored_namespaces = ["default"]
    if hasattr(config, "system") and hasattr(config.system, "topology") and hasattr(config.system.topology, "monitored_namespaces"):
        monitored_namespaces = config.system.topology.monitored_namespaces or monitored_namespaces

    # 1. Parse Helm charts using primary monitored namespace as default
    default_ns = monitored_namespaces[0] if monitored_namespaces else "default"
    parser = parse_helm_dir_fn or parse_helm_dir
    chart_nodes: List[TopologyNode] = []
    chart_edges: List[TopologyEdge] = []
    try:
        chart_nodes, chart_edges = parser(helm_dir, namespace=default_ns)
        logger.info("[TOPOLOGY SYNC] Parsed %d chart nodes from %s", len(chart_nodes), helm_dir)
    except Exception as exc:
        logger.warning("[TOPOLOGY SYNC] Failed parsing helm charts: %s", exc)

    # 2. Discover live Kubernetes workloads
    discovered_nodes: List[TopologyNode] = []
    discovered_edges: List[TopologyEdge] = []
    try:
        from services.api_server.topology.discovery import discover_cluster_topology
        discovered_nodes, discovered_edges = await discover_cluster_topology(monitored_namespaces)
        logger.info("[TOPOLOGY SYNC] Discovered %d cluster nodes in %s", len(discovered_nodes), monitored_namespaces)
    except Exception as exc:
        logger.warning("[TOPOLOGY SYNC] Cluster discovery failed: %s", exc)

    # 3. Merge static chart nodes + cluster nodes first so runtime discovery knows active services
    combined_nodes: Dict[str, TopologyNode] = {n.id: n for n in chart_nodes}
    for n in discovered_nodes:
        combined_nodes[n.id] = n

    # 4. Discover live runtime service-to-service edges from Jaeger / Tempo / Prometheus traces & spans
    runtime_nodes: List[TopologyNode] = []
    runtime_edges: List[TopologyEdge] = []
    try:
        from services.api_server.topology.discovery import discover_runtime_trace_topology
        sys_cfg = getattr(config, "system", config)
        mcp_endpoints = getattr(sys_cfg, "mcp_endpoints", None)
        runtime_nodes, runtime_edges = await discover_runtime_trace_topology(
            monitored_namespaces,
            mcp_endpoints=mcp_endpoints,
            existing_nodes=list(combined_nodes.values()),
        )
        logger.info(
            "[TOPOLOGY SYNC] Discovered %d runtime nodes and %d runtime trace/span edges",
            len(runtime_nodes),
            len(runtime_edges),
        )
    except Exception as exc:
        logger.warning("[TOPOLOGY SYNC] Runtime trace topology discovery skipped: %s", exc)

    for rn in runtime_nodes:
        if rn.id in combined_nodes:
            existing = combined_nodes[rn.id]
            merged_deps = sorted(set(existing.dependencies) | set(rn.dependencies))
            existing.dependencies = merged_deps
        else:
            combined_nodes[rn.id] = rn

    # 5. Merge edges without duplicates and update node.dependencies symmetrically
    edge_keys: Set[tuple] = set()
    combined_edges: List[TopologyEdge] = []
    for e in list(chart_edges) + list(discovered_edges) + list(runtime_edges):
        ek = (e.from_node, e.to_node, getattr(e, "port", None))
        if ek not in edge_keys:
            edge_keys.add(ek)
            combined_edges.append(e)
            if e.from_node in combined_nodes and e.to_node not in combined_nodes[e.from_node].dependencies:
                combined_nodes[e.from_node].dependencies.append(e.to_node)
                combined_nodes[e.from_node].dependencies.sort()

    return list(combined_nodes.values()), combined_edges


async def sync_topology_sources(
    topology_store: TopologyStore,
    config: Any,
    *,
    force: bool = False,
    updated_by: str = "system",
    parse_helm_dir_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Sync Helm charts and cluster discovery into TopologyStore, using SHA-256 digest versioning."""
    all_nodes_list, combined_edges = await build_live_topology(config, parse_helm_dir_fn=parse_helm_dir_fn)
    if not all_nodes_list:
        return {"changed": False, "sha256": "", "version": 0, "node_count": 0, "edge_count": 0, "namespaces": []}

    save_res = topology_store.save(all_nodes_list, combined_edges, updated_by=updated_by, force=force)
    clear_pending_drift(getattr(topology_store, "redis", None), "topology")
    namespaces = sorted(list({n.namespace for n in all_nodes_list if n.namespace}))
    logger.info(
        "[TOPOLOGY SYNC] %s v%s sha=%s (%d nodes, %d edges across %s)",
        "Saved" if save_res.get("changed") else "Unchanged",
        save_res.get("version"),
        str(save_res.get("sha256", ""))[:12],
        len(all_nodes_list),
        len(combined_edges),
        namespaces,
    )
    return {**save_res, "namespaces": namespaces}


async def detect_topology_drift(
    topology_store: TopologyStore,
    config: Any,
    redis_client: Optional[Any] = None,
    notify: bool = True,
) -> Optional[Dict[str, Any]]:
    """Compare live topology SHA-256 against stored snapshot and stage operator-gated drift if changed."""
    r_client = redis_client or getattr(topology_store, "redis", None)
    live_nodes, live_edges = await build_live_topology(config)
    if not live_nodes:
        return None

    live_sha = compute_topology_digest(live_nodes, live_edges)
    stored_meta = topology_store.get_meta()
    stored_nodes = topology_store.all_nodes()
    stored_edges = topology_store.all_edges()
    stored_sha = str(stored_meta.get("sha256") or "") or (
        compute_topology_digest(stored_nodes, stored_edges) if stored_nodes else ""
    )

    if live_sha == stored_sha:
        clear_pending_drift(r_client, "topology")
        return None

    stored_by_id = {n.id: n for n in stored_nodes}
    live_by_id = {n.id: n for n in live_nodes}
    added = sorted(set(live_by_id) - set(stored_by_id))
    removed = sorted(set(stored_by_id) - set(live_by_id))
    changed = sorted(
        nid for nid in (set(live_by_id) & set(stored_by_id))
        if live_by_id[nid].model_dump() != stored_by_id[nid].model_dump()
    )

    drift_record = {
        "target": "topology",
        "status": "pending",
        "old_sha": stored_sha,
        "new_sha": live_sha,
        "current_version": int(stored_meta.get("version") or 0),
        "summary": {
            "added_nodes": added,
            "removed_nodes": removed,
            "changed_nodes": changed,
            "live_node_count": len(live_nodes),
            "live_edge_count": len(live_edges),
        },
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    set_pending_drift(r_client, "topology", drift_record)
    await record_interaction_async(
        r_client,
        direction="internal",
        category="drift_detected",
        actor="topology_sync",
        entity_type="internal_service",
        target="topology",
        action="detect_drift",
        external=False,
        status="pending_approval",
        details={"old_sha": stored_sha[:12], "new_sha": live_sha[:12], "summary": drift_record["summary"]},
    )

    if notify:
        try:
            from services.api_server.notifications import dispatch_notification_background
            sys_cfg = getattr(config, "system", config)
            dispatch_notification_background(
                event="drift_detected",
                incident_id="system-drift-topology",
                data=drift_record,
                config=sys_cfg,
            )
        except Exception as exc:
            logger.debug("Failed dispatching topology drift notification: %s", exc)

    return drift_record

