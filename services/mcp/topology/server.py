import urllib.parse
from typing import Any, Dict, Optional
import redis
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from shared.models.topology import TopologyNode
from services.api_server.topology.store import TopologyStore
from services.api_server.topology.query import get_node
from shared.config.loader import ConfigManager


def _get_server_config():
    config = ConfigManager()
    server_url = (
        config.mcp.mcp_servers["topology"].server_url
        if "topology" in config.mcp.mcp_servers
        else "http://localhost:9008/mcp"
    )
    parsed = urllib.parse.urlparse(server_url)
    port = parsed.port or 9008
    host = "0.0.0.0"
    redis_url = config.system.redis.url
    snapshot_path = getattr(config.system.topology, "snapshot_path", "/data/topology_snapshot.json")
    max_budget = getattr(config.system.topology, "query_budget_per_incident", 5)
    return host, port, redis_url, snapshot_path, max_budget


_host, _port, _, _, _ = _get_server_config()
mcp = FastMCP("topology", host=_host, port=_port)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """
    Active health check probe for container orchestrators.
    Returns HTTP 200 if all service dependencies (Redis) are operational.
    Returns HTTP 503 if dependencies are unreachable.
    """
    _, _, redis_url, _, _ = _get_server_config()
    redis_status = "error"
    try:
        r = redis.from_url(redis_url, decode_responses=False)
        if r.ping():
            redis_status = "ok"
    except Exception as e:
        redis_status = f"unreachable: {e}"

    is_healthy = redis_status == "ok"
    status_code = 200 if is_healthy else 503
    return JSONResponse(
        {
            "status": "ok" if is_healthy else "degraded",
            "service": "topology-mcp",
            "redis": redis_status,
        },
        status_code=status_code,
    )


async def topology_query_node(
    node_id: str,
    incident_id: str,
    redis_client: Any = None,
    store: Optional[TopologyStore] = None,
    max_budget: int = 5,
) -> Dict[str, Any]:
    """
    Query a topology node for an incident with budget tracking.
    Raises ValueError if budget exceeded.
    Returns node dictionary (full node if found, stub node if not found).
    """
    if redis_client:
        key = f"topology:budget:{incident_id}"
        count = redis_client.incr(key)
        if count > max_budget:
            raise ValueError(
                f"Query budget exhausted for incident {incident_id} (call {count} exceeds limit {max_budget})"
            )

    node = get_node(store, node_id) if store else None
    if node:
        return node.model_dump()

    # Unknown node: return stub node
    ns = "default" if "." not in node_id else node_id.split(".")[-1]
    name = node_id.split(".")[0]
    stub = TopologyNode(id=node_id, name=name, namespace=ns, stub=True)
    return stub.model_dump()


@mcp.tool(name="topology_query_node")
async def mcp_topology_query_node(node_id: str, incident_id: str) -> Dict[str, Any]:
    """Query a topology node; enforces per-incident query budget."""
    config = ConfigManager()
    redis_url = config.system.redis.url
    r = redis.from_url(redis_url, decode_responses=False)
    snapshot_path = "/data/topology_snapshot.json"
    if hasattr(config.system, "topology") and hasattr(config.system.topology, "snapshot_path"):
        snapshot_path = config.system.topology.snapshot_path

    store = TopologyStore(r, snapshot_path=snapshot_path)
    max_budget = 5
    if hasattr(config.system, "topology") and hasattr(config.system.topology, "query_budget_per_incident"):
        max_budget = config.system.topology.query_budget_per_incident

    return await topology_query_node(
        node_id=node_id,
        incident_id=incident_id,
        redis_client=r,
        store=store,
        max_budget=max_budget,
    )


def register_tools(app: FastMCP) -> None:
    """Register topology tools onto a FastMCP instance."""
    @app.tool(name="topology_query_node")
    async def app_topology_query_node(node_id: str, incident_id: str) -> Dict[str, Any]:
        """Query a topology node; enforces per-incident query budget."""
        return await mcp_topology_query_node(node_id, incident_id)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
