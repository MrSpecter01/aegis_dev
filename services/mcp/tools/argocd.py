import re
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.config.loader import ConfigManager

APP_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_.]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


def validate_app_name(app: str) -> None:
    if not app or not app.strip():
        raise ValueError("ArgoCD application name cannot be empty")
    cleaned = app.strip()
    if len(cleaned) > 63:
        raise ValueError(f"Application name exceeds maximum length of 63 characters: '{app}'")
    if not APP_NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid ArgoCD application name: '{app}'. Must be alphanumeric with dashes, dots or underscores."
        )


def validate_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    return min(limit, 50)


def _get_argocd_config() -> tuple[str, Dict[str, str]]:
    config = ConfigManager()
    mcp_endpoints = config.system.mcp_endpoints
    url = mcp_endpoints.argocd_url.rstrip("/")
    headers = {}
    if mcp_endpoints.argocd_token:
        headers["Authorization"] = f"Bearer {mcp_endpoints.argocd_token}"
    return url, headers


async def argocd_get_app_status(
    app: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Retrieve application health and synchronization status from ArgoCD.
    Returns structured health, sync state, revision, and message.
    """
    validate_app_name(app)

    if base_url is None:
        resolved_url, resolved_headers = _get_argocd_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    if not base_url:
        return {
            "app": app,
            "configured": False,
            "health": "unknown",
            "sync": "unknown",
            "message": "ArgoCD integration not configured (ARGOCD_URL is empty)",
        }

    endpoint = f"{base_url.rstrip('/')}/api/v1/applications/{app}"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "app": app,
                    "configured": True,
                    "found": False,
                    "health": "not_found",
                    "sync": "not_found",
                    "message": f"Application '{app}' not found in ArgoCD",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"ArgoCD HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"ArgoCD connection error: {str(e)}") from e

        status = data.get("status", {})
        health = status.get("health", {})
        sync = status.get("sync", {})
        spec = data.get("spec", {})
        source = spec.get("source", {})

        return {
            "app": app,
            "configured": True,
            "found": True,
            "health": health.get("status", "Unknown"),
            "health_message": health.get("message", ""),
            "sync": sync.get("status", "Unknown"),
            "sync_revision": sync.get("revision", ""),
            "repo_url": source.get("repoURL", ""),
            "target_revision": source.get("targetRevision", ""),
            "path": source.get("path", ""),
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


async def argocd_get_sync_history(
    app: str,
    limit: int = 10,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Retrieve deployment/sync history for an application from ArgoCD.
    """
    validate_app_name(app)
    effective_limit = validate_limit(limit)

    if base_url is None:
        resolved_url, resolved_headers = _get_argocd_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    if not base_url:
        return {
            "app": app,
            "configured": False,
            "sync_count": 0,
            "history": [],
            "message": "ArgoCD integration not configured (ARGOCD_URL is empty)",
        }

    endpoint = f"{base_url.rstrip('/')}/api/v1/applications/{app}"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "app": app,
                    "configured": True,
                    "found": False,
                    "sync_count": 0,
                    "history": [],
                    "message": f"Application '{app}' not found in ArgoCD",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"ArgoCD HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"ArgoCD connection error: {str(e)}") from e

        history_items = data.get("status", {}).get("history", [])
        # Most recent first
        sorted_history = sorted(history_items, key=lambda h: h.get("id", 0), reverse=True)
        limited_history = sorted_history[:effective_limit]

        formatted_history: List[Dict[str, Any]] = []
        for item in limited_history:
            formatted_history.append({
                "id": item.get("id"),
                "revision": item.get("revision"),
                "deployed_at": item.get("deployedAt"),
                "source": item.get("source", {}),
            })

        return {
            "app": app,
            "configured": True,
            "found": True,
            "sync_count": len(formatted_history),
            "history": formatted_history,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register ArgoCD tools onto a FastMCP instance."""
    @mcp.tool(name="argocd_get_app_status")
    async def mcp_argocd_get_app_status(app: str) -> Dict[str, Any]:
        """Retrieve application health and sync status from ArgoCD."""
        return await argocd_get_app_status(app)

    @mcp.tool(name="argocd_get_sync_history")
    async def mcp_argocd_get_sync_history(app: str, limit: int = 10) -> Dict[str, Any]:
        """Retrieve recent sync and deployment history from ArgoCD."""
        return await argocd_get_sync_history(app, limit)
