import re
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.auth.registry import get_endpoint_auth_headers
from shared.config.loader import ConfigManager

LOGQL_SAFE_PATTERN = re.compile(r"^{[a-zA-Z0-9_:{}[\]().,|'\"~!=<>+\-*/%\\@#?& ]+$")
DURATION_PATTERN = re.compile(r"^(\d+[smhdw]|P(?:T?\d+[SMHDW]))$", re.IGNORECASE)
MAX_LIMIT = 500


def validate_logql_query(logql: str) -> None:
    """Validate LogQL query to ensure it starts with a stream selector and contains safe characters."""
    if not logql or not logql.strip():
        raise ValueError("LogQL query cannot be empty")
    cleaned = logql.strip()
    if not cleaned.startswith("{"):
        raise ValueError("LogQL query must start with a stream selector '{...'")
    if len(cleaned) > 1000:
        raise ValueError("LogQL query exceeds maximum length of 1000 characters")
    if any(c in cleaned for c in (";", "`", "$", "\x00", "\r", "\n")):
        raise ValueError("Invalid LogQL query: disallowed control or injection characters detected")
    if not LOGQL_SAFE_PATTERN.match(cleaned):
        raise ValueError(f"Invalid LogQL query: disallowed characters detected in '{cleaned}'")


def validate_limit(limit: int) -> int:
    """Validate and enforce server-side limit ceiling."""
    if limit < 1:
        raise ValueError(f"Limit must be at least 1, got {limit}")
    return min(limit, MAX_LIMIT)


def validate_time_range(time_range: str) -> None:
    """Validate time range format."""
    if not time_range or not time_range.strip():
        raise ValueError("time_range cannot be empty")
    if not DURATION_PATTERN.match(time_range.strip()):
        raise ValueError(
            f"Invalid time_range: '{time_range}'. Expected duration like '5m', '1h', '30s', or ISO8601 duration"
        )


async def loki_query(
    logql: str,
    time_range: str = "5m",
    limit: int = 50,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a LogQL query against Loki and return structured log lines.
    Enforces maximum limit cap of 500 server-side.
    """
    validate_logql_query(logql)
    validate_time_range(time_range)
    effective_limit = validate_limit(limit)

    if not base_url:
        config = ConfigManager()
        base_url = config.system.mcp_endpoints.loki_url
        auth_headers = get_endpoint_auth_headers("loki", config.system.mcp_endpoints)
    else:
        auth_headers = get_endpoint_auth_headers("loki")

    clean_base_url = base_url.rstrip("/")
    endpoint = f"{clean_base_url}/loki/api/v1/query_range"

    params = {
        "query": logql,
        "limit": effective_limit,
        "direction": "BACKWARD",
    }
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, params=params, timeout=10.0, **req_kwargs)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Loki HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Loki connection error: {str(e)}") from e

        if data.get("status") != "success":
            raise RuntimeError(f"Loki query error: {data.get('error', 'unknown error')}")

        results = data.get("data", {}).get("result", [])
        lines: List[str] = []
        stream_labels: Dict[str, str] = {}

        for item in results:
            labels = item.get("stream", {})
            stream_labels.update(labels)
            for entry in item.get("values", []):
                # entry is [timestamp_str, log_line_str]
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    lines.append(entry[1])
                elif isinstance(entry, str):
                    lines.append(entry)

        return {
            "query": logql,
            "time_range": time_range,
            "limit": effective_limit,
            "line_count": len(lines),
            "lines": lines,
            "stream_labels": stream_labels,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register Loki tools onto a FastMCP instance."""
    @mcp.tool(name="loki_query")
    async def mcp_loki_query(logql: str, time_range: str = "5m", limit: int = 50) -> Dict[str, Any]:
        """Execute a LogQL query against Loki with limit cap and validation."""
        return await loki_query(logql, time_range, limit)
