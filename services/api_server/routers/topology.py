from typing import Optional
from fastapi import APIRouter, Request, HTTPException

from services.api_server.auth_context import resolve_request_actor
from services.api_server.topology.query import get_node
from services.api_server.topology.parser import parse_helm_dir
from services.api_server.topology.sync import sync_topology_sources

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("")
async def get_full_topology(request: Request, namespace: Optional[str] = None):
    """Retrieve topology graph with nodes and edges, optionally filtered by namespace."""
    store = getattr(request.app.state, "topology_store", None)
    config = getattr(request.app.state, "config", None)
    if not store:
        return {"nodes": [], "edges": [], "namespaces": [], "observed_namespaces": []}

    all_nodes = store.all_nodes()
    all_edges = store.all_edges()

    node_namespaces = sorted(list({n.namespace for n in all_nodes if n.namespace}))

    # Determine actively observed namespaces
    monitored_namespaces = ["default"]
    if config and hasattr(config, "system") and hasattr(config.system, "topology"):
        cfg_monitored = getattr(config.system.topology, "monitored_namespaces", None)
        if cfg_monitored:
            monitored_namespaces = [ns for ns in cfg_monitored if ns != "default"]

    observed_namespaces = sorted(list({
        n.namespace for n in all_nodes
        if n.namespace in monitored_namespaces
    }))
    if not observed_namespaces:
        observed_namespaces = node_namespaces if node_namespaces else ["default"]

    # Target namespace: if explicit query param provided, use it; otherwise default to first observed namespace
    if namespace and namespace.strip().upper() == "ALL":
        target_ns = None
    elif namespace and namespace.strip():
        target_ns = namespace.strip().lower()
    else:
        target_ns = observed_namespaces[0]

    if target_ns:
        filtered_nodes = [n for n in all_nodes if (n.namespace or "").lower() == target_ns]
        node_ids = {n.id for n in filtered_nodes}
        filtered_edges = [
            e for e in all_edges
            if e.from_node in node_ids and e.to_node in node_ids
        ]
        return {
            "nodes": [n.model_dump() for n in filtered_nodes],
            "edges": [e.model_dump(by_alias=True) for e in filtered_edges],
            "namespaces": node_namespaces,
            "observed_namespaces": observed_namespaces,
            "current_namespace": target_ns,
        }

    return {
        "nodes": [n.model_dump() for n in all_nodes],
        "edges": [e.model_dump(by_alias=True) for e in all_edges],
        "namespaces": node_namespaces,
        "observed_namespaces": observed_namespaces,
        "current_namespace": "ALL",
    }


@router.get("/node/{node_id}")
async def get_topology_node(node_id: str, request: Request):
    """Retrieve details for a single topology node."""
    store = getattr(request.app.state, "topology_store", None)
    if not store:
        raise HTTPException(status_code=404, detail="Topology store not initialized")
    node = get_node(store, node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Topology node '{node_id}' not found")
    return node.model_dump()


@router.post("/refresh")
async def refresh_topology(request: Request, force: bool = False):
    """Re-sync Helm charts and Kubernetes cluster workloads into TopologyStore with SHA-256 versioning."""
    store = getattr(request.app.state, "topology_store", None)
    config = getattr(request.app.state, "config", None)

    if not store:
        return {
            "status": "error",
            "detail": "Topology store not initialized",
            "node_count": 0,
            "edge_count": 0,
            "namespaces": [],
        }

    actor, _ = resolve_request_actor(request, None, "dashboard")
    sync_res = await sync_topology_sources(
        store,
        config,
        force=force,
        updated_by=actor,
        parse_helm_dir_fn=parse_helm_dir,
    )
    return {
        "status": "refreshed",
        **sync_res,
    }
