"""Endpoints for SHA-256 digest status, operator-gated drift detection/approval, and manual/force sync."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scripts.seed_runbooks import (
    _read_disk_runbook_meta,
    _resolve_runbooks_paths,
    detect_runbook_drift,
    sync_runbook_index_and_qdrant,
)
from services.api_server.auth_context import resolve_request_actor
from services.api_server.topology.sync import detect_topology_drift, sync_topology_sources
from shared.audit.tracker import record_interaction_async
from shared.config.loader import ConfigManager
from shared.versioning import (
    RUNBOOKS_META_KEY,
    TOPOLOGY_META_KEY,
    clear_pending_drift,
    get_pending_drift,
    read_meta,
)

logger = logging.getLogger("aegis.api_server.drift")

router = APIRouter(prefix="/drift", tags=["drift"])

VALID_TARGETS = ("topology", "runbooks")


class DriftRejectRequest(BaseModel):
    reason: Optional[str] = "Rejected by operator"
    actor: str = "operator"
    channel: str = "dashboard"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None


class DriftActionRequest(BaseModel):
    actor: str = "operator"
    channel: str = "dashboard"
    force: bool = False
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None


def _validate_target(target: str) -> str:
    clean = (target or "").strip().lower()
    if clean not in VALID_TARGETS:
        raise HTTPException(status_code=400, detail=f"Invalid drift target '{target}'. Must be 'topology' or 'runbooks'.")
    return clean


async def _parse_optional_action_body(request: Request) -> DriftActionRequest:
    try:
        raw_bytes = await request.body()
        if raw_bytes:
            return DriftActionRequest.model_validate(json.loads(raw_bytes.decode("utf-8")))
    except Exception:
        pass
    return DriftActionRequest()


@router.get("/status")
async def get_drift_status(request: Request):
    """Return active SHA-256 digest metadata and any pending drift awaiting operator sign-off."""
    redis_client = getattr(request.app.state, "redis", None)
    store = getattr(request.app.state, "topology_store", None)

    topo_meta = store.get_meta() if store is not None else read_meta(redis_client, TOPOLOGY_META_KEY)
    runbooks_meta = read_meta(redis_client, RUNBOOKS_META_KEY)
    if not runbooks_meta:
        _, idx_file = _resolve_runbooks_paths()
        runbooks_meta = _read_disk_runbook_meta(idx_file)

    return {
        "topology_meta": topo_meta,
        "runbooks_meta": runbooks_meta,
        "pending": {
            "topology": get_pending_drift(redis_client, "topology"),
            "runbooks": get_pending_drift(redis_client, "runbooks"),
        },
    }


@router.post("/check")
async def check_drift(request: Request, target: Optional[str] = None):
    """Detect SHA-256 drift for topology and/or runbook index and alert operators without overwriting."""
    redis_client = getattr(request.app.state, "redis", None)
    store = getattr(request.app.state, "topology_store", None)
    config = getattr(request.app.state, "config", None) or ConfigManager()
    sys_cfg = getattr(config, "system", config)

    targets = [_validate_target(target)] if target else ["topology", "runbooks"]
    results: Dict[str, Any] = {}

    if "topology" in targets:
        if store is None:
            results["topology"] = {"status": "skipped", "detail": "Topology store not initialized"}
        else:
            drift = await detect_topology_drift(store, config, redis_client=redis_client, notify=True)
            results["topology"] = drift or {"status": "in_sync"}

    if "runbooks" in targets:
        drift = await asyncio.to_thread(
            detect_runbook_drift,
            None,
            redis_client,
            None,
            sys_cfg,
            True,
        )
        results["runbooks"] = drift or {"status": "in_sync"}

    return {
        "checked": targets,
        "results": results,
        "pending": {
            "topology": get_pending_drift(redis_client, "topology"),
            "runbooks": get_pending_drift(redis_client, "runbooks"),
        },
    }


@router.post("/{target}/approve")
async def approve_drift_update(target: str, request: Request):
    """Approve a pending drift alert and apply the SHA-versioned sync."""
    clean_target = _validate_target(target)
    redis_client = getattr(request.app.state, "redis", None)
    store = getattr(request.app.state, "topology_store", None)
    config = getattr(request.app.state, "config", None) or ConfigManager()
    sys_cfg = getattr(config, "system", config)

    pending = get_pending_drift(redis_client, clean_target)
    if not pending:
        raise HTTPException(status_code=404, detail=f"No pending drift awaiting approval for '{clean_target}'")

    body = await _parse_optional_action_body(request)
    actor, actor_meta = resolve_request_actor(
        request,
        body.actor,
        body.channel,
        body.telegram_user_id,
        body.telegram_username,
        body.telegram_full_name,
    )

    if clean_target == "topology":
        if store is None:
            raise HTTPException(status_code=503, detail="Topology store not initialized")
        sync_res = await sync_topology_sources(store, config, force=False, updated_by=actor)
    else:
        sync_res = await sync_runbook_index_and_qdrant(sys_cfg, redis_client=redis_client, force=False, updated_by=actor)

    clear_pending_drift(redis_client, clean_target)
    await record_interaction_async(
        redis_client,
        direction="inbound",
        category="drift_approved",
        actor=actor,
        entity_type=actor_meta.get("entity_type") or "operator",
        key_id=actor_meta.get("key_id"),
        target=clean_target,
        action="approve_drift",
        external=True,
        status="approved",
        details={"old_sha": pending.get("old_sha"), "new_sha": sync_res.get("sha256"), "version": sync_res.get("version")},
    )
    return {**sync_res, "status": "approved", "sync_status": sync_res.get("status"), "target": clean_target, "actor": actor}


@router.post("/{target}/reject")
async def reject_drift_update(target: str, request: Request):
    """Reject and clear a pending drift alert without modifying the active snapshot/index."""
    clean_target = _validate_target(target)
    redis_client = getattr(request.app.state, "redis", None)
    pending = get_pending_drift(redis_client, clean_target)
    if not pending:
        raise HTTPException(status_code=404, detail=f"No pending drift awaiting approval for '{clean_target}'")

    req_body = DriftRejectRequest()
    try:
        raw = await request.body()
        if raw:
            req_body = DriftRejectRequest.model_validate(json.loads(raw.decode("utf-8")))
    except Exception:
        pass

    actor, actor_meta = resolve_request_actor(
        request,
        req_body.actor,
        req_body.channel,
        req_body.telegram_user_id,
        req_body.telegram_username,
        req_body.telegram_full_name,
    )
    clear_pending_drift(redis_client, clean_target)
    await record_interaction_async(
        redis_client,
        direction="inbound",
        category="drift_rejected",
        actor=actor,
        entity_type=actor_meta.get("entity_type") or "operator",
        key_id=actor_meta.get("key_id"),
        target=clean_target,
        action="reject_drift",
        external=True,
        status="rejected",
        details={"reason": req_body.reason, "rejected_sha": pending.get("new_sha")},
    )
    return {"status": "rejected", "target": clean_target, "actor": actor, "reason": req_body.reason}


@router.post("/{target}/sync")
async def sync_target_manual(target: str, request: Request, force: bool = False):
    """Trigger manual sync or force rebuild for 'topology' or 'runbooks'."""
    clean_target = _validate_target(target)
    redis_client = getattr(request.app.state, "redis", None)
    store = getattr(request.app.state, "topology_store", None)
    config = getattr(request.app.state, "config", None) or ConfigManager()
    sys_cfg = getattr(config, "system", config)

    body = await _parse_optional_action_body(request)
    eff_force = bool(force or body.force)
    actor, actor_meta = resolve_request_actor(
        request,
        body.actor,
        body.channel,
        body.telegram_user_id,
        body.telegram_username,
        body.telegram_full_name,
    )

    if clean_target == "topology":
        if store is None:
            raise HTTPException(status_code=503, detail="Topology store not initialized")
        sync_res = await sync_topology_sources(store, config, force=eff_force, updated_by=actor)
    else:
        sync_res = await sync_runbook_index_and_qdrant(
            sys_cfg,
            redis_client=redis_client,
            force=eff_force,
            updated_by=actor,
        )

    await record_interaction_async(
        redis_client,
        direction="inbound",
        category="drift_force_rebuild" if eff_force else "drift_manual_sync",
        actor=actor,
        entity_type=actor_meta.get("entity_type") or "operator",
        key_id=actor_meta.get("key_id"),
        target=clean_target,
        action="force_rebuild" if eff_force else "manual_sync",
        external=True,
        status="ok",
        details={"force": eff_force, "changed": sync_res.get("changed"), "sha256": sync_res.get("sha256"), "version": sync_res.get("version")},
    )
    return {"target": clean_target, "force": eff_force, "actor": actor, **sync_res}

