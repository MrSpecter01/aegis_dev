import re
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.auth.registry import get_endpoint_auth_headers
from shared.config.loader import ConfigManager

SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")
TRACE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{16,32}$")
DURATION_PATTERN = re.compile(r"^(\d+[smhdw]|P(?:T?\d+[SMHDW]))$", re.IGNORECASE)


def validate_service_name(service: str) -> None:
    if not service or not service.strip():
        raise ValueError("Service name cannot be empty")
    if not SERVICE_NAME_PATTERN.match(service.strip()):
        raise ValueError(
            f"Invalid service name: '{service}'. Must be 1-100 alphanumeric characters, dots, dashes, or underscores."
        )


def validate_trace_id(trace_id: str) -> None:
    if not trace_id or not trace_id.strip():
        raise ValueError("trace_id cannot be empty")
    cleaned = trace_id.strip()
    if not TRACE_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid trace_id: '{trace_id}'. Must be 16 to 32 hexadecimal characters."
        )


def validate_duration_ms(min_duration_ms: int) -> None:
    if min_duration_ms < 0 or min_duration_ms > 600000:
        raise ValueError(
            f"min_duration_ms must be between 0 and 600000 ms, got {min_duration_ms}"
        )


def validate_time_range(time_range: str) -> None:
    if not time_range or not time_range.strip():
        raise ValueError("time_range cannot be empty")
    if not DURATION_PATTERN.match(time_range.strip()):
        raise ValueError(
            f"Invalid time_range: '{time_range}'. Expected duration like '5m', '1h', '30s', or ISO8601 duration"
        )


async def tempo_search(
    service: str,
    error: bool = False,
    min_duration_ms: int = 0,
    time_range: str = "5m",
    limit: int = 20,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search traces in Tempo by service, error state, and duration.
    Returns structured trace summaries.
    """
    validate_service_name(service)
    validate_duration_ms(min_duration_ms)
    validate_time_range(time_range)
    effective_limit = min(max(limit, 1), 100)

    if not base_url:
        config = ConfigManager()
        base_url = config.system.mcp_endpoints.tempo_url
        auth_headers = get_endpoint_auth_headers("tempo", config.system.mcp_endpoints)
    else:
        auth_headers = get_endpoint_auth_headers("tempo")

    clean_base_url = base_url.rstrip("/")
    endpoint = f"{clean_base_url}/api/search"
    params: Dict[str, Any] = {
        "q": f'{{resource.service.name="{service}"}}',
        "limit": effective_limit,
    }
    if error:
        params["tags"] = "status=error"
    if min_duration_ms > 0:
        params["minDuration"] = f"{min_duration_ms}ms"
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, params=params, timeout=10.0, **req_kwargs)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Tempo HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Tempo connection error: {str(e)}") from e

        raw_traces = data.get("traces", [])
        trace_summaries: List[Dict[str, Any]] = []
        for t in raw_traces:
            trace_summaries.append({
                "trace_id": t.get("traceID"),
                "root_service": t.get("rootServiceName"),
                "root_trace_name": t.get("rootTraceName"),
                "duration_ms": t.get("durationMs"),
                "start_time_unix_nano": t.get("startTimeUnixNano"),
            })

        if not raw_traces and "data" in data:
            for item in data.get("data", []):
                item_spans = item.get("spans", [])
                dur_ms = (item_spans[0].get("duration", 0) // 1000) if item_spans else 0
                trace_summaries.append({
                    "trace_id": item.get("traceID"),
                    "root_service": service,
                    "root_trace_name": item_spans[0].get("operationName", "") if item_spans else "",
                    "duration_ms": dur_ms,
                    "start_time_unix_nano": (item_spans[0].get("startTime", 0) * 1000) if item_spans else 0,
                })

        return {
            "service": service,
            "error_filtered": error,
            "min_duration_ms": min_duration_ms,
            "time_range": time_range,
            "trace_count": len(trace_summaries),
            "traces": trace_summaries,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


async def tempo_get_trace(
    trace_id: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve full trace details by hexadecimal trace_id.
    """
    validate_trace_id(trace_id)

    if not base_url:
        config = ConfigManager()
        base_url = config.system.mcp_endpoints.tempo_url
        auth_headers = get_endpoint_auth_headers("tempo", config.system.mcp_endpoints)
    else:
        auth_headers = get_endpoint_auth_headers("tempo")

    clean_base_url = base_url.rstrip("/")
    endpoint = f"{clean_base_url}/api/traces/{trace_id}"
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, timeout=10.0, **req_kwargs)
            if resp.status_code == 404:
                return {
                    "trace_id": trace_id,
                    "found": False,
                    "span_count": 0,
                    "spans": [],
                    "message": f"Trace {trace_id} not found in Tempo",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Tempo HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Tempo connection error: {str(e)}") from e

        # Parse spans from OTLP format or Tempo batches
        spans: List[Dict[str, Any]] = []
        batches = data.get("batches", [])
        for batch in batches:
            res_attrs = {
                attr.get("key"): attr.get("value", {}).get("stringValue")
                for attr in batch.get("resource", {}).get("attributes", [])
            }
            service_name = res_attrs.get("service.name", "unknown")
            scope_spans = batch.get("scopeSpans", [])
            for scope in scope_spans:
                for s in scope.get("spans", []):
                    spans.append({
                        "span_id": s.get("spanId"),
                        "parent_span_id": s.get("parentSpanId"),
                        "name": s.get("name"),
                        "service_name": service_name,
                        "status": s.get("status", {}),
                        "start_time_unix_nano": s.get("startTimeUnixNano"),
                        "end_time_unix_nano": s.get("endTimeUnixNano"),
                    })

        if not batches and "data" in data:
            for item in data.get("data", []):
                processes = item.get("processes", {})
                for s in item.get("spans", []):
                    pid = s.get("processID")
                    proc = processes.get(pid, {})
                    svc_name = proc.get("serviceName", "unknown")
                    refs = s.get("references", [])
                    parent_id = refs[0].get("spanID") if refs else None
                    tags = s.get("tags", [])
                    has_error = any(t.get("key") == "error" and t.get("value") is True for t in tags)
                    spans.append({
                        "span_id": s.get("spanID"),
                        "parent_span_id": parent_id,
                        "name": s.get("operationName"),
                        "service_name": svc_name,
                        "status": {"code": 2 if has_error else 0},
                        "start_time_unix_nano": s.get("startTime", 0) * 1000,
                        "end_time_unix_nano": (s.get("startTime", 0) + s.get("duration", 0)) * 1000,
                    })

        return {
            "trace_id": trace_id,
            "found": True,
            "span_count": len(spans),
            "spans": spans,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register Tempo tools onto a FastMCP instance."""
    @mcp.tool(name="tempo_search")
    async def mcp_tempo_search(
        service: str,
        error: bool = False,
        min_duration_ms: int = 0,
        time_range: str = "5m",
    ) -> Dict[str, Any]:
        """Search traces in Tempo by service, error state, and duration."""
        return await tempo_search(service, error, min_duration_ms, time_range)

    @mcp.tool(name="tempo_get_trace")
    async def mcp_tempo_get_trace(trace_id: str) -> Dict[str, Any]:
        """Retrieve full trace details and spans for a given trace_id."""
        return await tempo_get_trace(trace_id)
