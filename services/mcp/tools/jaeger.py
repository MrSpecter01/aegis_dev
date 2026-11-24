"""Jaeger distributed tracing tools for Aegis FastMCP server.

Provides native search and trace retrieval from Jaeger Query REST API.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Union
import httpx
from mcp.server.fastmcp import FastMCP

from shared.auth.registry import get_endpoint_auth_headers
from shared.config.loader import ConfigManager

logger = logging.getLogger("aegis.mcp.tools.jaeger")

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


def _resolve_jaeger_endpoint(base_url: Optional[str] = None, path: str = "api/traces") -> str:
    """Build full Jaeger API endpoint from configured jaeger_url."""
    if not base_url:
        config = ConfigManager()
        base_url = config.system.mcp_endpoints.jaeger_url or config.system.mcp_endpoints.tempo_url

    clean_base = base_url.rstrip("/")
    # If base url ends in /api/traces, don't duplicate
    if clean_base.endswith("/api/traces"):
        if path == "api/traces":
            return clean_base
        return f"{clean_base.rsplit('/api/traces', 1)[0]}/{path}"
    return f"{clean_base}/{path}"


async def jaeger_search_traces(
    service: str,
    operation: Optional[str] = None,
    tags: Optional[Union[str, Dict[str, Any]]] = None,
    min_duration_ms: int = 0,
    lookback: str = "1h",
    limit: int = 20,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search traces in Jaeger by service, operation, tags, and minimum duration.
    Returns structured summaries of matched traces.
    """
    validate_service_name(service)
    validate_duration_ms(min_duration_ms)
    effective_limit = min(max(limit, 1), 100)

    endpoint = _resolve_jaeger_endpoint(base_url, "api/traces")
    auth_headers = get_endpoint_auth_headers("jaeger")
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}
    params: Dict[str, Any] = {
        "service": service.strip(),
        "limit": effective_limit,
        "lookback": lookback,
    }
    if operation and operation.strip():
        params["operation"] = operation.strip()
    if isinstance(tags, dict) and tags:
        params["tags"] = json.dumps(tags)
    elif isinstance(tags, str) and tags.strip():
        # Jaeger accepts tags as JSON format or key=value strings
        t_str = tags.strip()
        if not t_str.startswith("{"):
            if "=" in t_str:
                k, v = t_str.split("=", 1)
                t_str = json.dumps({k.strip(): v.strip()})
        params["tags"] = t_str
    if min_duration_ms > 0:
        params["minDuration"] = f"{min_duration_ms}ms"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, params=params, timeout=10.0, **req_kwargs)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Jaeger HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Jaeger connection error: {str(e)}") from e

        raw_traces = data.get("data", [])
        summaries: List[Dict[str, Any]] = []

        for trace in raw_traces:
            trace_id = trace.get("traceID", "")
            spans = trace.get("spans", [])
            processes = trace.get("processes", {})
            span_count = len(spans)

            root_span_name = "unknown"
            root_service = service
            has_error = False
            duration_us = 0
            start_time_us = 0

            for s in spans:
                # Root span usually has no references or empty references
                if not s.get("references"):
                    root_span_name = s.get("operationName", root_span_name)
                    pid = s.get("processID")
                    if pid and pid in processes:
                        root_service = processes[pid].get("serviceName", root_service)
                    duration_us = max(duration_us, s.get("duration", 0))
                    start_time_us = s.get("startTime", 0)

                # Check tags for errors
                for t in s.get("tags", []):
                    if t.get("key") == "error" and (t.get("value") is True or str(t.get("value")).lower() == "true"):
                        has_error = True
                    elif t.get("key") == "http.status_code" and str(t.get("value")).startswith("5"):
                        has_error = True

            duration_ms = duration_us / 1000.0 if duration_us else 0.0

            summaries.append({
                "trace_id": trace_id,
                "root_service": root_service,
                "root_span_name": root_span_name,
                "start_time_unix_nano": start_time_us * 1000,
                "duration_ms": duration_ms,
                "span_count": span_count,
                "has_error": has_error,
            })

        return {
            "service": service,
            "total_matches": len(summaries),
            "traces": summaries,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


async def jaeger_get_trace(
    trace_id: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve full trace details, spans, error tags, and durations from Jaeger by trace_id.
    """
    validate_trace_id(trace_id)
    endpoint = _resolve_jaeger_endpoint(base_url, f"api/traces/{trace_id.strip()}")
    auth_headers = get_endpoint_auth_headers("jaeger")
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, timeout=10.0, **req_kwargs)
            if resp.status_code == 404:
                return {
                    "trace_id": trace_id,
                    "found": False,
                    "error": f"Trace '{trace_id}' not found in Jaeger",
                    "spans": [],
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Jaeger HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Jaeger connection error: {str(e)}") from e

        trace_data = data.get("data", [])
        if not trace_data:
            return {
                "trace_id": trace_id,
                "found": False,
                "spans": [],
            }

        trace = trace_data[0]
        spans: List[Dict[str, Any]] = []
        processes = trace.get("processes", {})

        for s in trace.get("spans", []):
            pid = s.get("processID")
            svc_name = processes.get(pid, {}).get("serviceName", "unknown") if pid else "unknown"
            refs = s.get("references", [])
            parent_id = refs[0].get("spanID") if refs else None

            tags = s.get("tags", [])
            has_error = any(
                t.get("key") == "error" and (t.get("value") is True or str(t.get("value")).lower() == "true")
                for t in tags
            )

            spans.append({
                "span_id": s.get("spanID"),
                "parent_span_id": parent_id,
                "name": s.get("operationName"),
                "service_name": svc_name,
                "status": {"code": 2 if has_error else 0},
                "start_time_unix_nano": s.get("startTime", 0) * 1000,
                "duration_ms": s.get("duration", 0) / 1000.0,
                "tags": {t.get("key"): t.get("value") for t in tags},
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
    """Register Jaeger tools onto a FastMCP instance."""

    @mcp.tool(name="jaeger_search_traces")
    async def mcp_jaeger_search_traces(
        service: str,
        operation: Optional[str] = None,
        tags: Optional[str] = None,
        min_duration_ms: int = 0,
        lookback: str = "1h",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Search distributed traces in Jaeger for a given service.
        Supports filtering by operation name, tags (e.g. 'error=true'), minimum duration in ms, and lookback time.
        Returns trace summaries including root service, duration, error state, and span counts.
        """
        return await jaeger_search_traces(
            service=service,
            operation=operation,
            tags=tags,
            min_duration_ms=min_duration_ms,
            lookback=lookback,
            limit=limit,
        )

    @mcp.tool(name="jaeger_get_trace")
    async def mcp_jaeger_get_trace(trace_id: str) -> Dict[str, Any]:
        """
        Retrieve full distributed trace details and spans from Jaeger for a given trace_id (16-32 hex characters).
        Returns all span metadata, parent-child relations, error status, and durations.
        """
        return await jaeger_get_trace(trace_id=trace_id)

