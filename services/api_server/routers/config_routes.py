import difflib
import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from shared.auth.registry import IdentityRegistry
from shared.config.loader import (
    ConfigManager,
    DEFAULT_CONFIG_DIR,
    get_config,
)
from shared.config.schemas import (
    AgentsConfig,
    SAGPoliciesConfig,
    MCPConfig,
    SystemConfig,
)
from services.api_server.auth_context import resolve_request_actor

logger = logging.getLogger("aegis.api_server.config")

router = APIRouter()

MAX_HISTORY_VERSIONS_PER_FILE = 8


def _get_config_dir() -> Path:
    env_dir = os.getenv("AEGIS_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.exists():
            return p
    return DEFAULT_CONFIG_DIR


def _get_history_dir() -> Path:
    h_dir = _get_config_dir() / "history"
    h_dir.mkdir(parents=True, exist_ok=True)
    return h_dir


def _prune_history_manifest(h_dir: Path, manifest: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep at most MAX_HISTORY_VERSIONS_PER_FILE entries per config file and delete evicted YAML snapshots."""
    kept: List[Dict[str, Any]] = []
    evicted: List[Dict[str, Any]] = []
    counts_by_file: Dict[str, int] = {}

    for item in manifest:
        if not isinstance(item, dict):
            continue
        file_key = str(item.get("file") or "")
        count = counts_by_file.get(file_key, 0)
        if count < MAX_HISTORY_VERSIONS_PER_FILE:
            kept.append(item)
            counts_by_file[file_key] = count + 1
        else:
            evicted.append(item)

    kept_paths = {str(item.get("path") or "") for item in kept}
    h_resolved = h_dir.resolve()
    for item in evicted:
        rel_path = str(item.get("path") or "")
        if not rel_path or rel_path in kept_paths:
            continue
        candidate = (h_dir / rel_path).resolve()
        if candidate.parent == h_resolved and candidate.name != "manifest.json" and candidate.is_file():
            try:
                candidate.unlink()
            except Exception as exc:
                logger.warning("Failed deleting evicted config history snapshot %s: %s", candidate.name, exc)
    return kept


def _save_version_snapshot(filename: str, description: str = "", actor: Optional[str] = None) -> str:
    """Save current content of a config file into config/history/ before modification (bounded to 8 per file)."""
    cfg_dir = _get_config_dir()
    src_file = cfg_dir / filename
    if not src_file.exists():
        return ""

    h_dir = _get_history_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_version_id = f"{src_file.stem}_{ts}"
    version_id = base_version_id
    seq = 2
    while (h_dir / f"{version_id}.yaml").exists():
        version_id = f"{base_version_id}_{seq}"
        seq += 1
    dest_file = h_dir / f"{version_id}.yaml"

    try:
        content = src_file.read_text(encoding="utf-8")
        dest_file.write_text(content, encoding="utf-8")

        manifest_path = h_dir / "manifest.json"
        manifest: List[Dict[str, Any]] = []
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    manifest = loaded
            except Exception:
                manifest = []

        entry: Dict[str, Any] = {
            "version_id": version_id,
            "file": filename,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "description": description or f"Auto-backup before update of {filename}",
            "path": str(dest_file.name),
        }
        if actor:
            entry["actor"] = actor
        manifest.insert(0, entry)

        pruned = _prune_history_manifest(h_dir, manifest)
        manifest_path.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
        return version_id
    except Exception as exc:
        logger.warning("Failed to save config version snapshot for %s: %s", filename, exc)
        return ""


def _list_history(file_filter: str) -> List[Dict[str, Any]]:
    manifest_path = _get_history_dir() / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return [item for item in data if item.get("file") == file_filter][:MAX_HISTORY_VERSIONS_PER_FILE]
    except Exception:
        return []


class RollbackRequest(BaseModel):
    version_id: str


@router.get("/history/version/{version_id}")
async def get_version_details(version_id: str):
    h_dir = _get_history_dir()
    snap_path = h_dir / f"{version_id}.yaml"
    if not snap_path.exists():
        raise HTTPException(status_code=404, detail=f"Version '{version_id}' not found")

    manifest_path = h_dir / "manifest.json"
    manifest_item: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            items = json.loads(manifest_path.read_text(encoding="utf-8"))
            for it in items:
                if it.get("version_id") == version_id:
                    manifest_item = it
                    break
        except Exception:
            pass

    file_name = manifest_item.get("file", f"{version_id.split('_')[0]}.yaml")
    active_path = _get_config_dir() / file_name
    active_content = active_path.read_text(encoding="utf-8") if active_path.exists() else ""
    historical_content = snap_path.read_text(encoding="utf-8")

    # Compute unified diff
    diff_lines = list(difflib.unified_diff(
        active_content.splitlines(keepends=True),
        historical_content.splitlines(keepends=True),
        fromfile=f"current/{file_name}",
        tofile=f"snapshot/{version_id}.yaml",
    ))
    diff_text = "".join(diff_lines)

    historical_parsed = {}
    active_parsed = {}
    try:
        historical_parsed = yaml.safe_load(historical_content) or {}
    except Exception:
        pass
    try:
        active_parsed = yaml.safe_load(active_content) or {}
    except Exception:
        pass

    return {
        "version_id": version_id,
        "file": file_name,
        "timestamp": manifest_item.get("timestamp", ""),
        "description": manifest_item.get("description", ""),
        "raw_content": historical_content,
        "current_content": active_content,
        "diff": diff_text,
        "parsed": historical_parsed,
        "current_parsed": active_parsed,
    }


# ============================================================================
# SYSTEM CONFIG
# ============================================================================

def _redact_system_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Redact API keys and tokens from serialized system config responses."""
    if isinstance(data.get("system"), dict) and "api_key" in data["system"]:
        data["system"]["api_key"] = "***"
    if isinstance(data.get("telegram"), dict) and data["telegram"].get("bot_token"):
        data["telegram"]["bot_token"] = "***"
    if isinstance(data.get("providers"), dict):
        for k in ("google_api_key", "openai_api_key", "anthropic_api_key"):
            if data["providers"].get(k):
                data["providers"][k] = "***"
    if isinstance(data.get("mcp_endpoints"), dict):
        for k in (
            "prometheus_api_key",
            "loki_api_key",
            "tempo_api_key",
            "jaeger_api_key",
            "flagd_api_key",
            "kubernetes_token",
            "argocd_token",
            "github_token",
        ):
            if data["mcp_endpoints"].get(k):
                data["mcp_endpoints"][k] = "***"
    return data


@router.get("")
async def get_system_config():
    config = ConfigManager(_get_config_dir())
    data = config.system.model_dump()
    return _redact_system_secrets(data)


@router.get("/identities")
async def get_identities_config():
    """Return non-secret view of registered operators, Telegram mappings, webhooks, and services."""
    config = ConfigManager(_get_config_dir())
    registry = IdentityRegistry.from_config(
        getattr(config, "identities", None),
        legacy_api_key=getattr(getattr(config.system, "system", None), "api_key", ""),
    )
    return registry.list_public_identities()


@router.put("/system")
async def update_system_config(request: Request):
    body = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "system.yaml"

    # Validate schema
    try:
        SystemConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid system.yaml schema: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("system.yaml", "Updated system configuration", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(body, f, sort_keys=False)

    get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "config": body}


# ============================================================================
# PROVIDER KEYS & ENDPOINTS CONFIG
# ============================================================================

def _mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "********"
    return f"{key[:4]}...{key[-4:]}"


@router.get("/providers")
async def get_providers_config():
    config = ConfigManager(_get_config_dir())
    prov = getattr(config.system, "providers", None)
    google_key = (prov.google_api_key if prov else None) or os.getenv("GOOGLE_API_KEY", "")
    openai_key = (prov.openai_api_key if prov else None) or os.getenv("OPENAI_API_KEY", "")
    anthropic_key = (prov.anthropic_api_key if prov else None) or os.getenv("ANTHROPIC_API_KEY", "")
    ollama_url = (prov.ollama_url if prov else None) or os.getenv("OLLAMA_URL", "") or (config.system.ollama.url if config.system else "")

    return {
        "google_api_key_masked": _mask_key(google_key),
        "google_api_key_set": bool(google_key),
        "openai_api_key_masked": _mask_key(openai_key),
        "openai_api_key_set": bool(openai_key),
        "anthropic_api_key_masked": _mask_key(anthropic_key),
        "anthropic_api_key_set": bool(anthropic_key),
        "ollama_url": ollama_url,
    }


@router.put("/providers")
async def update_providers_config(request: Request):
    patch = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "system.yaml"

    current = {}
    if target_path.exists():
        with open(target_path, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}

    providers = current.setdefault("providers", {})
    if "google_api_key" in patch and patch["google_api_key"] and not patch["google_api_key"].startswith("AIza..."):
        providers["google_api_key"] = patch["google_api_key"].strip()
        os.environ["GOOGLE_API_KEY"] = patch["google_api_key"].strip()
    if "openai_api_key" in patch and patch["openai_api_key"] and not patch["openai_api_key"].startswith("sk-..."):
        providers["openai_api_key"] = patch["openai_api_key"].strip()
        os.environ["OPENAI_API_KEY"] = patch["openai_api_key"].strip()
    if "anthropic_api_key" in patch and patch["anthropic_api_key"]:
        providers["anthropic_api_key"] = patch["anthropic_api_key"].strip()
        os.environ["ANTHROPIC_API_KEY"] = patch["anthropic_api_key"].strip()
    if "ollama_url" in patch and patch["ollama_url"]:
        providers["ollama_url"] = patch["ollama_url"].strip()
        os.environ["OLLAMA_URL"] = patch["ollama_url"].strip()

    # Validate schema
    try:
        SystemConfig.model_validate(current)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid system.yaml schema: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("system.yaml", "Updated LLM Provider Keys and endpoints", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, sort_keys=False)

    get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "providers": providers}


# ============================================================================
# AGENTS CONFIG & VERSIONING
# ============================================================================

@router.get("/agents")
async def get_agents_config():
    config = ConfigManager(_get_config_dir())
    return config.agents.model_dump()


@router.get("/agents/history")
async def get_agents_history():
    return _list_history("agents.yaml")


@router.post("/agents/rollback")
async def rollback_agents_config(req: RollbackRequest, request: Request):
    cfg_dir = _get_config_dir()
    snapshot_path = _get_history_dir() / f"{req.version_id}.yaml"
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail=f"Version '{req.version_id}' not found")

    target_path = cfg_dir / "agents.yaml"
    actor, _ = resolve_request_actor(request, None, "dashboard")
    # Backup current before restoring
    _save_version_snapshot("agents.yaml", f"Backup prior to rolling back to {req.version_id}", actor=actor)

    content = snapshot_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    try:
        AgentsConfig.model_validate(parsed)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Historical file invalid: {exc}")

    target_path.write_text(content, encoding="utf-8")
    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "rolled_back", "version_id": req.version_id, "agents": updated.agents.model_dump()}


@router.put("/agents/{agent_id}")
async def update_agent_config(agent_id: str, request: Request):
    patch = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "agents.yaml"

    current = {}
    if target_path.exists():
        with open(target_path, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}

    agents = current.setdefault("agents", {})
    if agent_id not in agents:
        agents[agent_id] = {}

    # Merge patch into agent config
    if isinstance(patch, dict):
        for k, v in patch.items():
            if k == "primary" and isinstance(v, dict):
                agents[agent_id].setdefault("primary", {}).update(v)
            else:
                agents[agent_id][k] = v

    # Validate against Pydantic schema
    try:
        AgentsConfig.model_validate(current)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid agents configuration: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("agents.yaml", f"Updated agent '{agent_id}' configuration", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, sort_keys=False)

    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "agent_id": agent_id, "config": agents[agent_id], "all_agents": updated.agents.model_dump()}


@router.put("/agents")
async def update_all_agents(request: Request):
    body = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "agents.yaml"

    try:
        AgentsConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid agents configuration: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("agents.yaml", "Batch update of agents.yaml", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(body, f, sort_keys=False)

    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "all_agents": updated.agents.model_dump()}


# ============================================================================
# SAG POLICIES CONFIG & VERSIONING
# ============================================================================

@router.get("/sag")
async def get_sag_config():
    config = ConfigManager(_get_config_dir())
    return config.sag.model_dump()


@router.get("/sag/history")
async def get_sag_history():
    return _list_history("sag_policies.yaml")


@router.post("/sag/rollback")
async def rollback_sag_config(req: RollbackRequest, request: Request):
    cfg_dir = _get_config_dir()
    snapshot_path = _get_history_dir() / f"{req.version_id}.yaml"
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail=f"Version '{req.version_id}' not found")

    target_path = cfg_dir / "sag_policies.yaml"
    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("sag_policies.yaml", f"Backup prior to rolling back to {req.version_id}", actor=actor)

    content = snapshot_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    try:
        SAGPoliciesConfig.model_validate(parsed)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Historical file invalid: {exc}")

    target_path.write_text(content, encoding="utf-8")
    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "rolled_back", "version_id": req.version_id, "sag": updated.sag.model_dump()}


@router.put("/sag/tools/{tool_name}")
async def update_sag_tool_policy(tool_name: str, request: Request):
    policy_patch = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "sag_policies.yaml"

    current = {}
    if target_path.exists():
        with open(target_path, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}

    policies = current.setdefault("policies", {})
    tools = policies.setdefault("tools", {})
    tool_cfg = tools.setdefault(tool_name, {})

    if isinstance(policy_patch, dict):
        tool_cfg.update(policy_patch)

    try:
        SAGPoliciesConfig.model_validate(current)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid SAG policy: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("sag_policies.yaml", f"Updated policy for tool '{tool_name}'", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, sort_keys=False)

    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "tool_name": tool_name, "policy": tool_cfg, "sag": updated.sag.model_dump()}


@router.put("/sag")
async def update_all_sag_policies(request: Request):
    body = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "sag_policies.yaml"

    try:
        SAGPoliciesConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid SAG policies: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("sag_policies.yaml", "Batch update of sag_policies.yaml", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(body, f, sort_keys=False)

    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "sag": updated.sag.model_dump()}


# ============================================================================
# MCP CONFIG
# ============================================================================

@router.get("/mcp")
async def get_mcp_config():
    config = ConfigManager(_get_config_dir())
    return config.mcp.model_dump()


@router.put("/mcp")
async def update_mcp_config(request: Request):
    body = await request.json()
    cfg_dir = _get_config_dir()
    target_path = cfg_dir / "mcp.yaml"

    try:
        MCPConfig.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid mcp.yaml configuration: {exc}")

    actor, _ = resolve_request_actor(request, None, "dashboard")
    _save_version_snapshot("mcp.yaml", "Updated registered MCP servers configuration", actor=actor)

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(body, f, sort_keys=False)

    updated = get_config(config_dir=cfg_dir, reload=True)
    return {"status": "updated", "mcp": updated.mcp.model_dump()}
