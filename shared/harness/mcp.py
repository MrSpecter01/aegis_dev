import asyncio
from contextlib import asynccontextmanager
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import uuid
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from shared.audit.tracker import (
    get_audit_context,
    get_audit_redis_client,
    record_interaction_async,
)
from shared.auth.registry import build_auth_headers
from shared.config.schemas import MCPServerConfig, MCPConfig

_INTERNAL_HOSTS = {
    "",
    "localhost",
    "127.0.0.1",
    "::1",
    "redis",
    "qdrant",
    "prometheus",
    "loki",
    "tempo",
    "jaeger",
    "flagd",
    "aegis-mcp",
    "aegis-topology-mcp",
    "aegis-api",
    "aegis-prometheus-mcp",
    "aegis-loki-mcp",
    "aegis-tempo-mcp",
    "aegis-jaeger-mcp",
    "aegis-k8s-mcp",
    "aegis-argocd-mcp",
    "aegis-github-mcp",
}


def _is_external_url(url: Optional[str]) -> bool:
    if not url:
        return False
    parsed = urlparse(str(url).strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host not in _INTERNAL_HOSTS


class MCPClient:
    """Unified async client supporting Streamable HTTP, SSE, and STDIO MCP servers."""

    def __init__(
        self,
        server_url: str = "",
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        transport: str = "streamable_http",
        auth_type: str = "bearer",
        auth_header: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        server_name: str = "",
        external: bool = False,
        api_key_env: Optional[str] = None,
    ):
        self.server_url = (server_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport
        self.auth_type = auth_type
        self.auth_header = auth_header
        self.headers = dict(headers) if headers else {}
        self.command = command
        self.args = list(args) if args else []
        self.env = dict(env) if env else {}
        self.cwd = cwd
        self.server_name = server_name
        self.external = bool(external)
        self.api_key_env = api_key_env

    def _get_headers(self) -> Optional[Dict[str, str]]:
        merged = build_auth_headers(
            api_key=self.api_key,
            auth_type=self.auth_type,
            auth_header=self.auth_header,
            extra_headers=self.headers,
        )
        return merged or None

    @asynccontextmanager
    async def _open_streams(self) -> AsyncIterator[Tuple[Any, Any]]:
        headers = self._get_headers()
        if self.transport in ("streamable_http", "http"):
            async with streamablehttp_client(
                url=self.server_url, headers=headers, timeout=self.timeout
            ) as (read_stream, write_stream, _):
                yield read_stream, write_stream
        elif self.transport == "sse":
            async with sse_client(
                url=self.server_url, headers=headers, timeout=self.timeout
            ) as (read_stream, write_stream):
                yield read_stream, write_stream
        elif self.transport == "stdio":
            if not self.command:
                raise ValueError("command is required for stdio MCP transport")
            proc_env = {**os.environ, **self.env} if self.env else None
            params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=proc_env,
                cwd=self.cwd,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                yield read_stream, write_stream
        else:
            raise ValueError(f"Unsupported MCP transport: {self.transport}")

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Return all tool definitions registered on this MCP server."""
        async with self._open_streams() as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [t.model_dump() for t in result.tools]

    async def get_tool(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Return the definition for a single named tool, or None if not found."""
        tools = await self.list_tools()
        for tool in tools:
            if tool.get("name") == tool_name:
                return tool
        return None

    def _resolve_tool_audit_info(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Determine target endpoint, external boundary, read/write mode, and credential reference (never secret values)."""
        tool_mode = "read"
        target = self.server_url or self.command or self.server_name or tool_name
        is_external = self.external or _is_external_url(self.server_url)
        credential_ref = (
            f"env:{self.api_key_env}"
            if self.api_key_env
            else ("configured" if self.api_key else "none")
        )

        try:
            from shared.config.loader import ConfigManager
            cfg = ConfigManager()
            ep = cfg.system.mcp_endpoints
            sag_tool = cfg.sag.tools.get(tool_name)
            if sag_tool is not None:
                tool_mode = "write" if sag_tool.requires_approval else "read"
            elif tool_name in ("restart_pod", "scale_deployment", "rollback_release", "update_resource_limits", "drain_node", "toggle_feature_flag"):
                tool_mode = "write"

            if not self.external:
                if tool_name.startswith("prometheus"):
                    target = ep.prometheus_url or target
                    is_external = is_external or _is_external_url(ep.prometheus_url)
                    if ep.prometheus_api_key:
                        credential_ref = "env:PROMETHEUS_API_KEY"
                elif tool_name.startswith("loki"):
                    target = ep.loki_url or target
                    is_external = is_external or _is_external_url(ep.loki_url)
                    if ep.loki_api_key:
                        credential_ref = "env:LOKI_API_KEY"
                elif tool_name.startswith("tempo"):
                    target = ep.tempo_url or target
                    is_external = is_external or _is_external_url(ep.tempo_url)
                    if ep.tempo_api_key:
                        credential_ref = "env:TEMPO_API_KEY"
                elif tool_name.startswith("jaeger"):
                    target = ep.jaeger_url or ep.tempo_url or target
                    is_external = is_external or _is_external_url(target)
                    if ep.jaeger_api_key:
                        credential_ref = "env:JAEGER_API_KEY"
                elif tool_name.startswith("github"):
                    target = "https://api.github.com"
                    is_external = True
                    credential_ref = "env:GITHUB_TOKEN" if ep.github_token else "none"
                elif tool_name.startswith("argocd"):
                    target = ep.argocd_url or "argocd"
                    is_external = True
                    credential_ref = "env:ARGOCD_TOKEN" if ep.argocd_token else "none"
                elif tool_name.startswith("k8s_") or tool_name in (
                    "restart_pod", "scale_deployment", "rollback_release", "update_resource_limits", "drain_node"
                ):
                    target = ep.kubernetes_url or "kubernetes-cluster"
                    is_external = True
                    if ep.kubernetes_token:
                        credential_ref = "env:KUBERNETES_TOKEN"
                    elif ep.kubeconfig_path:
                        credential_ref = "kubeconfig"
                    else:
                        credential_ref = "in-cluster-sa"
                elif tool_name in ("get_feature_flag", "toggle_feature_flag"):
                    target = ep.flagd_url or "flagd"
                    is_external = True
                    credential_ref = "env:FLAGD_API_KEY" if ep.flagd_api_key else "none"
                elif tool_name.startswith("topology_") or tool_name.startswith("qdrant_"):
                    is_external = False
                    credential_ref = "internal"
        except Exception:
            pass

        return {
            "tool_mode": tool_mode,
            "target": str(target),
            "is_external": bool(is_external),
            "credential_ref": credential_ref,
        }

    async def _emit_tool_audit_async(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        status_label: str,
        latency_ms: float,
        err_msg: Optional[str],
    ) -> None:
        try:
            ctx = get_audit_context()
            redis_client = get_audit_redis_client()
            incident_id = ctx.get("incident_id") or arguments.get("incident_id")
            caller = ctx.get("caller") or "mcp-caller"
            info = self._resolve_tool_audit_info(tool_name, arguments)

            await record_interaction_async(
                redis_client,
                direction="outbound",
                category=f"mcp_tool_{info['tool_mode']}",
                actor=str(caller),
                entity_type="internal_service",
                credential_ref=info["credential_ref"],
                target=info["target"],
                action=tool_name,
                external=info["is_external"],
                status=status_label,
                incident_id=str(incident_id) if incident_id else None,
                latency_ms=latency_ms,
                details={
                    "server": self.server_name or tool_name,
                    "tool_mode": info["tool_mode"],
                    "transport": self.transport,
                    **({"error": err_msg[:200]} if err_msg else {}),
                },
            )

            if (
                redis_client is not None
                and incident_id
                and (info["is_external"] or info["tool_mode"] == "write")
            ):
                from shared.ledger.writer import append_ledger_entry
                from shared.models.enums import IncidentState
                from shared.models.ledger import LedgerEntry

                state_val = IncidentState.EXECUTING if info["tool_mode"] == "write" else IncidentState.TRIAGE
                entry = LedgerEntry(
                    entry_id=f"led-ext-{uuid.uuid4().hex[:8]}",
                    incident_id=str(incident_id),
                    state=state_val,
                    event_type="external_tool_call",
                    source=str(caller),
                    details={
                        "tool": tool_name,
                        "tool_mode": info["tool_mode"],
                        "target": info["target"],
                        "external": info["is_external"],
                        "credential_ref": info["credential_ref"],
                        "status": status_label,
                        "latency_ms": round(latency_ms, 2),
                    },
                )
                await asyncio.to_thread(append_ledger_entry, redis_client, str(incident_id), entry)
        except Exception:
            pass

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke a tool by name with the given arguments and return the result."""
        t0 = time.perf_counter()
        status_label = "ok"
        err_msg: Optional[str] = None
        try:
            async with self._open_streams() as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    dumped = result.model_dump()
                    if isinstance(dumped, dict) and dumped.get("isError"):
                        status_label = "tool_error"
                    return dumped
        except Exception as exc:
            status_label = "error"
            err_msg = str(exc)
            raise
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            await self._emit_tool_audit_async(tool_name, arguments, status_label, latency_ms, err_msg)


def build_mcp_client(server_config: MCPServerConfig, server_name: str = "") -> MCPClient:
    """Construct an MCPClient from a server config entry."""
    return MCPClient(
        server_url=server_config.server_url,
        api_key=server_config.api_key,
        timeout=server_config.timeout,
        transport=server_config.transport,
        auth_type=server_config.auth_type,
        auth_header=server_config.auth_header,
        headers=server_config.headers,
        command=server_config.command,
        args=server_config.args,
        env=server_config.env,
        cwd=server_config.cwd,
        server_name=server_name,
        external=getattr(server_config, "external", False),
        api_key_env=getattr(server_config, "api_key_env", None),
    )


def get_mcp_client(server_name: str, config: Optional[MCPConfig] = None) -> MCPClient:
    """Fetch server configuration and build an MCPClient for the named server."""
    if config is None:
        from shared.config.loader import load_mcp_config
        config = load_mcp_config()
    if server_name not in config.mcp_servers:
        raise KeyError(f"MCP server '{server_name}' not found in mcp.yaml config")
    return build_mcp_client(config.mcp_servers[server_name], server_name=server_name)


def get_mcp_clients(
    server_names: Optional[List[str]] = None,
    config: Optional[MCPConfig] = None,
) -> Dict[str, MCPClient]:
    """Build a dictionary of MCPClients from mcp.yaml config.
    
    If server_names is specified, only builds clients for the requested servers.
    Otherwise builds clients for all configured servers in mcp.yaml.
    """
    if config is None:
        from shared.config.loader import load_mcp_config
        config = load_mcp_config()
    
    targets = server_names if server_names is not None else list(config.mcp_servers.keys())
    clients: Dict[str, MCPClient] = {}
    for name in targets:
        if name in config.mcp_servers:
            clients[name] = build_mcp_client(config.mcp_servers[name], server_name=name)
    return clients


def resolve_mcp_client_for_tool(
    tool_name: str,
    config: Optional[MCPConfig] = None,
) -> MCPClient:
    """Find the configured MCP server responsible for the given tool_name and return its client.
    
    Inspects server tool mappings in mcp.yaml. If no explicit tool mapping matches,
    checks for a unified 'aegis-mcp' / default server or raises KeyError.
    """
    if config is None:
        from shared.config.loader import load_mcp_config
        config = load_mcp_config()

    # 1. Search servers with explicit tool declarations
    for server_name, server_cfg in config.mcp_servers.items():
        if tool_name in getattr(server_cfg, "tool_names", server_cfg.tools):
            return build_mcp_client(server_cfg, server_name=server_name)

    # 2. Check if a unified FastMCP server is available
    if "aegis" in config.mcp_servers:
        return build_mcp_client(config.mcp_servers["aegis"], server_name="aegis")
    if "aegis-mcp" in config.mcp_servers:
        return build_mcp_client(config.mcp_servers["aegis-mcp"], server_name="aegis-mcp")

    raise KeyError(f"No MCP server found in mcp.yaml configured to handle tool '{tool_name}'")

