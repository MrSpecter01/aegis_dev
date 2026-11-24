import json
import os
from typing import Any, Dict, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.auth.registry import get_endpoint_auth_headers
from shared.config.loader import ConfigManager
from shared.sag.validator import (
    check_injection,
    get_redis_client,
    validate_environment,
    validate_feature_flag_name,
    verify_action_approved,
)


def _resolve_flagd_url(flagd_url: Optional[str] = None) -> str:
    """Resolve flagd-ui base URL from explicit arg, env var, or system config."""
    if flagd_url is not None:
        return flagd_url.strip().rstrip("/")
    env_url = os.getenv("AEGIS_FLAGD_URL") or os.getenv("FLAGD_UI_URL")
    if env_url:
        return env_url.strip().rstrip("/")
    try:
        return (ConfigManager().system.mcp_endpoints.flagd_url or "").strip().rstrip("/")
    except Exception:
        return ""


def _resolve_target_variant(flag_def: Dict[str, Any], enabled: bool, variant: Optional[str]) -> str:
    """Resolve target variant name in flagd schema for enable/disable actions."""
    variants = flag_def.get("variants") or {}
    if variant is not None and variant.strip():
        chosen = variant.strip()
        if variants and chosen not in variants:
            raise ValueError(
                f"Variant '{chosen}' not found for flag. Available variants: {list(variants.keys())}"
            )
        return chosen

    if not enabled:
        if "off" in variants:
            return "off"
        for k, v in variants.items():
            if v in (False, 0, "off", "disabled"):
                return k
        return "off"

    if "on" in variants:
        return "on"
    for k in variants:
        if k != "off":
            return k
    return "on"


async def get_feature_flag(
    flag_name: Optional[str] = None,
    flagd_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Inspect live feature flag state from flagd-ui."""
    if flag_name is not None and flag_name.strip():
        try:
            validate_feature_flag_name(flag_name)
            check_injection({"flag_name": flag_name})
        except ValueError as exc:
            return {"success": False, "error": str(exc), "isError": True}

    base_url = _resolve_flagd_url(flagd_url)
    if not base_url:
        return {
            "success": False,
            "isError": True,
            "error": "flagd_url is not configured in system.mcp_endpoints",
        }

    read_url = f"{base_url}/api/read-file"
    auth_headers = get_endpoint_auth_headers("flagd")
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _fetch(c: httpx.AsyncClient) -> Dict[str, Any]:
        resp = await c.get(read_url, timeout=10.0, **req_kwargs)
        resp.raise_for_status()
        data = resp.json()
        flags = data.get("flags", {})
        if flag_name and flag_name.strip():
            key = flag_name.strip()
            if key not in flags:
                return {
                    "success": False,
                    "isError": True,
                    "error": f"Feature flag '{key}' not found. Available: {sorted(flags.keys())}",
                }
            return {
                "success": True,
                "flag_name": key,
                "flag": flags[key],
            }

        active_flags = {
            k: v for k, v in flags.items()
            if isinstance(v, dict) and v.get("defaultVariant") not in (None, "off")
        }
        return {
            "success": True,
            "total_flags": len(flags),
            "active_flags": active_flags,
            "flags": {
                k: {
                    "defaultVariant": v.get("defaultVariant"),
                    "state": v.get("state"),
                    "variants": v.get("variants"),
                    "description": v.get("description"),
                }
                for k, v in flags.items()
                if isinstance(v, dict)
            },
        }

    try:
        if client is not None:
            return await _fetch(client)
        async with httpx.AsyncClient() as c:
            return await _fetch(c)
    except httpx.HTTPStatusError as exc:
        return {
            "success": False,
            "isError": True,
            "error": f"flagd-ui HTTP {exc.response.status_code}: {exc.response.text}",
        }
    except httpx.RequestError as exc:
        return {
            "success": False,
            "isError": True,
            "error": f"Failed connecting to flagd-ui at {base_url}: {exc}",
        }


async def toggle_feature_flag(
    flag_name: str,
    enabled: bool,
    environment: str,
    incident_id: str,
    action_id: str,
    variant: Optional[str] = None,
    redis_client: Optional[Any] = None,
    flagd_url: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Toggle a feature flag on or off in a target environment via flagd-ui."""
    try:
        validate_feature_flag_name(flag_name)
        validate_environment(environment)
        check_injection({
            "flag_name": flag_name,
            "environment": environment,
            "incident_id": incident_id,
            "action_id": action_id,
            **({"variant": variant} if variant is not None else {}),
        })
        r_client = redis_client if redis_client is not None else await get_redis_client()
        await verify_action_approved(r_client, incident_id, action_id, tool_name="toggle_feature_flag")
    except (ValueError, PermissionError) as exc:
        return {"success": False, "error": str(exc), "isError": True}

    base_url = _resolve_flagd_url(flagd_url)
    if not base_url:
        return {
            "success": False,
            "isError": True,
            "error": "flagd_url is not configured in system.mcp_endpoints",
        }

    clean_flag = flag_name.strip()
    read_url = f"{base_url}/api/read-file"
    write_url = f"{base_url}/api/write-to-file"
    auth_headers = get_endpoint_auth_headers("flagd")
    req_kwargs: Dict[str, Any] = {"headers": auth_headers} if auth_headers else {}

    async def _apply(c: httpx.AsyncClient) -> Dict[str, Any]:
        read_resp = await c.get(read_url, timeout=10.0, **req_kwargs)
        read_resp.raise_for_status()
        config_data = read_resp.json()
        flags = config_data.get("flags", {})
        if clean_flag not in flags:
            return {
                "success": False,
                "isError": True,
                "error": f"Feature flag '{clean_flag}' not found in flagd. Available: {sorted(flags.keys())}",
            }

        previous_variant = flags[clean_flag].get("defaultVariant")
        try:
            target_variant = _resolve_target_variant(flags[clean_flag], enabled, variant)
        except ValueError as exc:
            return {"success": False, "isError": True, "error": str(exc)}

        flags[clean_flag]["defaultVariant"] = target_variant
        config_data["flags"] = flags

        write_resp = await c.post(write_url, json={"data": config_data}, timeout=10.0, **req_kwargs)
        write_resp.raise_for_status()

        return {
            "success": True,
            "flag_name": clean_flag,
            "enabled": enabled,
            "previous_variant": previous_variant,
            "variant": target_variant,
            "environment": environment,
            "action_id": action_id,
            "message": (
                f"Feature flag '{clean_flag}' updated from '{previous_variant}' to "
                f"'{target_variant}' (enabled={enabled}) in {environment}."
            ),
        }

    try:
        if client is not None:
            return await _apply(client)
        async with httpx.AsyncClient() as c:
            return await _apply(c)
    except httpx.HTTPStatusError as exc:
        return {
            "success": False,
            "isError": True,
            "error": f"flagd-ui HTTP {exc.response.status_code}: {exc.response.text}",
        }
    except httpx.RequestError as exc:
        return {
            "success": False,
            "isError": True,
            "error": f"Failed connecting to flagd-ui at {base_url}: {exc}",
        }
    except json.JSONDecodeError as exc:
        return {
            "success": False,
            "isError": True,
            "error": f"Invalid JSON response from flagd-ui: {exc}",
        }


def register_tools(mcp: FastMCP) -> None:
    """Register feature flags tools with FastMCP server."""

    @mcp.tool(
        name="get_feature_flag",
        description="Inspect current live feature flag configurations and active failure toggles in flagd.",
    )
    async def _get_feature_flag(flag_name: Optional[str] = None) -> Dict[str, Any]:
        return await get_feature_flag(flag_name=flag_name)

    @mcp.tool(
        name="toggle_feature_flag",
        description="Toggle a feature flag on or off (or set a specific variant) in flagd for a target environment.",
    )
    async def _toggle_feature_flag(
        flag_name: str,
        enabled: bool,
        environment: str,
        incident_id: str,
        action_id: str,
        variant: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await toggle_feature_flag(
            flag_name=flag_name,
            enabled=enabled,
            environment=environment,
            incident_id=incident_id,
            action_id=action_id,
            variant=variant,
        )
