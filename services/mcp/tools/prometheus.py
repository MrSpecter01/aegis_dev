import re
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.auth.registry import get_endpoint_auth_headers
from shared.config.loader import ConfigManager

# PromQL safe characters regex: alphanumeric, underscores, colons, parentheses, metric labels, math/comparators
PROMQL_SAFE_PATTERN = re.compile(r"^[a-zA-Z_:(][a-zA-Z0-9_:{}[\]().,|'\"~!=<>+\-*/%^ ]*$")
# Duration pattern: standard Prometheus duration (e.g. 30s, 5m, 1h, 2d) or ISO8601 (e.g. PT5M, P1D)
DURATION_PATTERN = re.compile(r"^(\d+[smhdw]|P(?:T?\d+[SMHDW]))$", re.IGNORECASE)


def validate_promql_query(query: str) -> None:
    """Validate query to prevent injection and malformed queries."""
    if not query or not query.strip():
        raise ValueError("PromQL query cannot be empty")
    if len(query) > 1000:
        raise ValueError("PromQL query exceeds maximum length of 1000 characters")
    if not PROMQL_SAFE_PATTERN.match(query.strip()):
        raise ValueError(f"Invalid PromQL query: disallowed characters detected in '{query}'")


def validate_time_range(time_range: str) -> None:
    """Validate time range format."""
    if not time_range or not time_range.strip():
        raise ValueError("time_range cannot be empty")
    if not DURATION_PATTERN.match(time_range.strip()):
        raise ValueError(
            f"Invalid time_range: '{time_range}'. Expected duration like '5m', '1h', '30s', or ISO8601 duration"
        )


async def prometheus_query(
    query: str,
    time_range: str = "5m",
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a PromQL query against Prometheus and return structured metrics.
    Validates input characters and duration format.
    """
    validate_promql_query(query)
    validate_time_range(time_range)

    if not base_url:
        config = ConfigManager()
        base_url = config.system.mcp_endpoints.prometheus_url
        auth_headers = get_endpoint_auth_headers("prometheus", config.system.mcp_endpoints)
    else:
        auth_headers = get_endpoint_auth_headers("prometheus")

    clean_base_url = base_url.rstrip("/")
    endpoint = f"{clean_base_url}/api/v1/query"

    # If query does not contain range selector and time_range is provided, we query instant or subquery
    params = {"query": query}
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, params=params, timeout=10.0, **req_kwargs)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Prometheus HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Prometheus connection error: {str(e)}") from e

        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus query error: {data.get('error', 'unknown error')}")

        results = data.get("data", {}).get("result", [])
        formatted_results: List[Dict[str, Any]] = []
        for item in results:
            metric_labels = item.get("metric", {})
            values = item.get("values")
            if values is None and "value" in item:
                values = [item.get("value")]
            formatted_results.append({
                "metric": metric_labels,
                "values": values or [],
            })

        return {
            "query": query,
            "time_range": time_range,
            "result_count": len(formatted_results),
            "results": formatted_results,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register Prometheus tools onto a FastMCP instance."""
    @mcp.tool(name="prometheus_query")
    async def mcp_prometheus_query(query: str, time_range: str = "5m") -> Dict[str, Any]:
        """Execute a PromQL query against Prometheus with input validation."""
        return await prometheus_query(query, time_range)
