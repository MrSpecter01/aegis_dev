import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ValidationError

from shared.audit.tracker import list_dropped_alerts, record_dropped_alert_async
from shared.models.alerts import Alert, AlertGroup
from services.api_server.auth_context import resolve_request_actor
from services.api_server.incident_store import (
    generate_incident_id,
    create_incident,
    get_incident,
    list_incidents
)
from services.api_server.state_machine import load_machine

logger = logging.getLogger("aegis.api_server.routers.incidents")

router = APIRouter()

class TransitionRequest(BaseModel):
    event: str
    actor: str
    metadata: Optional[Dict[str, Any]] = None

class EscalateRequest(BaseModel):
    reason: str
    actor: str = "operator"
    channel: str = "dashboard"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None

class CloseRequest(BaseModel):
    reason: str
    generate_report: bool = False
    actor: str = "operator"
    channel: str = "dashboard"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None

class DropRequest(BaseModel):
    reason: Optional[str] = "Incident dropped by operator"
    actor: str = "operator"
    channel: str = "dashboard"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None

class ApprovalDecisionRequest(BaseModel):
    action_id: str
    decision: str  # "approved", "denied", or "steer"
    actor: str = "operator"
    channel: str = "dashboard"  # "telegram" or "dashboard"
    reason: Optional[str] = None
    instruction: Optional[str] = None  # Required when decision is "steer"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None

class SteerRequest(BaseModel):
    instruction: str
    actor: str = "operator"
    channel: str = "dashboard"
    action_id: Optional[str] = None
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None

class PauseRequest(BaseModel):
    reason: str
    actor: str = "operator"
    channel: str = "dashboard"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None

class ResumeRequest(BaseModel):
    instruction: Optional[str] = None
    actor: str = "operator"
    channel: str = "dashboard"
    telegram_user_id: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_full_name: Optional[str] = None


def _attach_actor_audit(meta: Dict[str, Any], actor_meta: Dict[str, Any]) -> Dict[str, Any]:
    """Attach identity attribution metadata to state-transition metadata for named keys or Telegram users."""
    enriched = dict(meta)
    key_id = actor_meta.get("key_id")
    if key_id and key_id != "legacy-api-key":
        enriched["auth_key_id"] = key_id
        if actor_meta.get("entity_type"):
            enriched["entity_type"] = actor_meta["entity_type"]
    if actor_meta.get("telegram_user_id"):
        enriched["telegram_user_id"] = actor_meta["telegram_user_id"]
    if actor_meta.get("operator_id"):
        enriched["operator_id"] = actor_meta["operator_id"]
    return enriched


def _verify_not_emergency_halt(redis_client: Optional[Any]) -> None:
    if not redis_client:
        return
    raw = redis_client.get("system:status:paused")
    if not raw:
        return
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
        if data.get("paused"):
            reason = data.get("reason", "Operator emergency halt active")
            raise HTTPException(
                status_code=423,
                detail=f"System is in Fleet-Wide Emergency Standby: '{reason}'. Action execution, approvals, and incident mutations are locked. Resume fleet to proceed.",
            )
    except HTTPException:
        raise
    except Exception:
        pass


def _resolve_allowed_namespaces(request: Request) -> Optional[list[str]]:
    cfg = getattr(request.app.state, "config", None)
    sys_cfg = getattr(cfg, "system", cfg) if cfg is not None else None
    if sys_cfg is None:
        return None
    ingestion_cfg = getattr(sys_cfg, "ingestion", None)
    topo_cfg = getattr(sys_cfg, "topology", None)

    raw_ing = getattr(ingestion_cfg, "allowed_namespaces", None) if ingestion_cfg else None
    if isinstance(raw_ing, list) and len(raw_ing) > 0:
        cleaned = [str(ns).strip().lower() for ns in raw_ing if str(ns).strip()]
        return cleaned or None

    raw_topo = getattr(topo_cfg, "monitored_namespaces", None) if topo_cfg else None
    if isinstance(raw_topo, list) and len(raw_topo) > 0:
        cleaned = [str(ns).strip().lower() for ns in raw_topo if str(ns).strip()]
        return cleaned or None
    return None


@router.post("/alerts/ingest")
async def ingest_alert(request: Request, payload: Dict[str, Any]):
    alerts = payload.get("alerts", [])
    if not alerts:
        raise HTTPException(status_code=400, detail="No alerts in payload")

    validated_alerts: list[tuple[Dict[str, Any], Alert]] = []
    try:
        for a in alerts:
            validated_alerts.append((a, Alert.model_validate(a)))
    except (ValidationError, Exception) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid alert: {exc}")

    redis_client = getattr(request.app.state, "redis", None)
    principal = getattr(request.state, "principal", None)
    allowed_ns = _resolve_allowed_namespaces(request)

    kept_raw: list[Dict[str, Any]] = []
    dropped_alerts: list[Alert] = []
    if allowed_ns:
        allowed_set = set(allowed_ns)
        for raw_a, parsed_a in validated_alerts:
            if parsed_a.namespace.strip().lower() in allowed_set:
                kept_raw.append(raw_a)
            else:
                dropped_alerts.append(parsed_a)
                await record_dropped_alert_async(
                    redis_client,
                    alertname=parsed_a.alertname,
                    service=parsed_a.service,
                    namespace=parsed_a.namespace,
                    allowed_namespaces=allowed_ns,
                    fingerprint=parsed_a.fingerprint or "",
                    ingested_by=principal.username if principal else "",
                    key_id=(principal.key_id or "") if principal else "",
                    reason="namespace_not_monitored",
                )
    else:
        kept_raw = [raw_a for raw_a, _ in validated_alerts]

    if dropped_alerts and not kept_raw:
        return {
            "status": "dropped_out_of_scope",
            "dropped_count": len(dropped_alerts),
            "dropped_namespaces": sorted({a.namespace for a in dropped_alerts}),
            "allowed_namespaces": allowed_ns,
        }

    if redis_client is None:
        raise HTTPException(status_code=503, detail="Alert queue unavailable")

    payload_to_enqueue = payload if not dropped_alerts else {**payload, "alerts": kept_raw}
    stream_fields: Dict[str, Any] = {"payload": json.dumps(payload_to_enqueue)}
    if principal is not None:
        stream_fields["ingested_by"] = principal.username
        stream_fields["ingested_by_id"] = principal.id
        stream_fields["ingested_entity_type"] = principal.entity_type
        if principal.key_id:
            stream_fields["ingested_key_id"] = principal.key_id

    msg_id = await asyncio.to_thread(redis_client.xadd, "stream:alert:raw", stream_fields)
    stream_id = msg_id if isinstance(msg_id, str) else msg_id.decode() if hasattr(msg_id, "decode") else str(msg_id)
    resp_body: Dict[str, Any] = {"status": "queued", "stream_id": stream_id}
    if dropped_alerts:
        resp_body["dropped_count"] = len(dropped_alerts)
    if principal is not None:
        resp_body["ingested_by"] = principal.username
        resp_body["key_id"] = principal.key_id
    return resp_body


@router.get("/alerts/dropped")
async def get_dropped_alerts(request: Request, limit: int = 100, namespace: Optional[str] = None):
    """List alerts dropped due to out-of-scope namespace filtering."""
    redis_client = getattr(request.app.state, "redis", None)
    items = await asyncio.to_thread(list_dropped_alerts, redis_client, limit=limit, namespace=namespace)
    return {"count": len(items), "items": items}

@router.get("/incidents")
async def get_incidents(request: Request, state: str = None, severity: str = None):
    redis_client = getattr(request.app.state, "redis", None)
    return await asyncio.to_thread(list_incidents, redis_client, state, severity)

@router.get("/incidents/{incident_id}")
async def get_incident_detail(incident_id: str, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    incident = await asyncio.to_thread(get_incident, redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    if redis_client is not None:
        raw_ctx = await asyncio.to_thread(redis_client.get, f"incident:{incident_id}:context")
        if raw_ctx:
            if isinstance(raw_ctx, (bytes, bytearray)):
                raw_ctx = raw_ctx.decode("utf-8")
            try:
                incident["context"] = json.loads(raw_ctx)
            except Exception:
                pass

        raw_act = await asyncio.to_thread(redis_client.get, f"incident:{incident_id}:action")
        if raw_act:
            if isinstance(raw_act, (bytes, bytearray)):
                raw_act = raw_act.decode("utf-8")
            try:
                incident["action"] = json.loads(raw_act)
            except Exception:
                pass
    return incident

@router.get("/incidents/{incident_id}/ledger")
async def get_incident_ledger(incident_id: str, request: Request):
    from shared.ledger.writer import get_ledger_entries
    redis_client = getattr(request.app.state, "redis", None)
    incident = await asyncio.to_thread(get_incident, redis_client, incident_id)
    if incident:
        incident_id = incident.get("incident_id", incident_id)
    entries = await asyncio.to_thread(get_ledger_entries, redis_client, incident_id)
    return [e.model_dump(mode="json") for e in entries]

@router.get("/incidents/{incident_id}/report")
async def get_incident_report(incident_id: str, request: Request):
    from pathlib import Path
    redis_client = getattr(request.app.state, "redis", None)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    candidate_paths = [
        Path(f"/data/incidents/{incident_id}/report.md"),
        Path.cwd() / "data" / "incidents" / incident_id / "report.md",
    ]
    try:
        from shared.config.loader import load_system_config
        storage = load_system_config().ledger.storage_path
        candidate_paths.insert(0, Path(storage) / incident_id / "report.md")
    except Exception:
        pass

    for p in candidate_paths:
        if p.exists() and p.is_file():
            try:
                content = p.read_text(encoding="utf-8")
                return {
                    "incident_id": incident_id,
                    "exists": True,
                    "content": content,
                    "path": str(p),
                }
            except Exception as exc:
                logger.warning("Failed to read report file %s: %s", p, exc)

    return {
        "incident_id": incident_id,
        "exists": False,
        "content": None,
        "message": f"No post-incident report found for '{incident_id}'.",
    }

@router.post("/incidents/{incident_id}/transition")
async def transition_incident(incident_id: str, req: TransitionRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    _verify_not_emergency_halt(redis_client)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)
        
    machine = load_machine(incident_id, redis_client)
    from_state = machine.state
    
    if req.event == "action_proposed":
        from shared.models.actions import ActionProposal
        from services.api_server.sag.approval import emit_approval_request
        raw_prop = (req.metadata or {}).get("proposal") or {}
        try:
            proposal = ActionProposal.model_validate(raw_prop)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid ActionProposal: {exc.errors()[0]['msg']}")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid ActionProposal: {exc}")
        if req.metadata is None:
            req.metadata = {}
        req.metadata["proposal"] = proposal.model_dump(mode="json")
        req.metadata["runbook_citations"] = [c.model_dump(mode="json") for c in proposal.runbook_citations]
        sev = incident.get("severity") or "P2"
        pre_approved = bool((req.metadata or {}).get("pre_approved", False))
        act_id = (req.metadata or {}).get("action_id")
        await emit_approval_request(
            redis_client,
            incident_id,
            proposal,
            severity=sev,
            action_id=act_id,
            schedule_timeout=not pre_approved,
            auto_approve=pre_approved,
        )

    try:
        await machine.trigger(req.event, req.actor, req.metadata)
        return {"from": from_state, "to": machine.state}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/incidents/{incident_id}/escalate")
async def escalate_incident(incident_id: str, req: EscalateRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    _verify_not_emergency_halt(redis_client)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)
        
    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )
    machine = load_machine(incident_id, redis_client)
    try:
        meta = _attach_actor_audit({"reason": req.reason, "channel": req.channel}, actor_meta)
        await machine.trigger("escalate", actor, meta)
        return {"status": "escalated", "actor": actor}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/incidents/{incident_id}/close")
async def close_incident(incident_id: str, req: CloseRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )
    machine = load_machine(incident_id, redis_client)
    meta = _attach_actor_audit({"reason": req.reason, "channel": req.channel}, actor_meta)
    try:
        if req.generate_report:
            # Transition to REPORTING so Phase 3 Scribe generates postmortem report and delivers it before closing
            await machine.trigger("generate_report", actor, meta)
            return {"status": "reporting", "actor": actor, "message": "Incident transitioned to REPORTING. Postmortem generation in progress."}
        else:
            # manual close fires force_close to force transition to CLOSED from any state
            await machine.trigger("force_close", actor, meta)
            return {"status": "closed", "actor": actor}
    except ValueError as e:
        if req.generate_report:
            # Fallback to force_close if generate_report transition fails
            try:
                await machine.trigger("force_close", actor, meta)
                return {"status": "closed", "actor": actor}
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/incidents/{incident_id}/drop")
async def drop_incident(incident_id: str, req: DropRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    machine = load_machine(incident_id, redis_client)
    if machine.state == "CLOSED":
        raise HTTPException(status_code=400, detail="Incident is already closed")

    reason = req.reason or "Incident dropped by operator"
    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )

    # Set dropped flag in Redis to stop workers immediately
    if redis_client is not None:
        drop_payload = json.dumps({
            "dropped": True,
            "dropped_at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "auth_key_id": actor_meta.get("key_id"),
            "channel": req.channel,
            "reason": reason,
        })
        redis_client.set(f"incident:{incident_id}:dropped", drop_payload)
        # Cancel any active action proposal
        raw_act = redis_client.get(f"incident:{incident_id}:action")
        if raw_act:
            try:
                act_data = json.loads(raw_act) if isinstance(raw_act, str) else raw_act
                if act_data.get("status") in ("pending", "approved"):
                    act_data["status"] = "cancelled"
                    act_data["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                    act_data["reason"] = f"Incident dropped: {reason}"
                    saved = json.dumps(act_data)
                    redis_client.set(f"incident:{incident_id}:action", saved)
                    act_id = act_data.get("action_id")
                    if act_id:
                        redis_client.set(f"incident:{incident_id}:action:{act_id}", saved)
                        if hasattr(redis_client, "srem"):
                            redis_client.srem(f"incident:{incident_id}:approved_actions", act_id)
            except Exception:
                pass

    try:
        meta = _attach_actor_audit({"reason": reason, "action": "dropped", "channel": req.channel}, actor_meta)
        await machine.trigger("drop", actor, meta)
        return {
            "status": "dropped",
            "incident_id": incident_id,
            "state": machine.state,
            "actor": actor,
            "message": f"Incident {incident_id} dropped successfully.",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/incidents/{incident_id}/pause")
async def pause_incident(incident_id: str, req: PauseRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    _verify_not_emergency_halt(redis_client)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )
    machine = load_machine(incident_id, redis_client)
    try:
        meta = _attach_actor_audit({"reason": req.reason, "channel": req.channel}, actor_meta)
        await machine.trigger("pause", actor, meta)
        return {"status": "paused", "incident_id": incident_id, "state": machine.state, "actor": actor}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/incidents/{incident_id}/resume")
async def resume_incident(incident_id: str, req: ResumeRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    _verify_not_emergency_halt(redis_client)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )
    pre_pause_state = None
    if redis_client is not None:
        raw_pre = redis_client.get(f"incident:{incident_id}:pre_pause_state")
        if raw_pre:
            if isinstance(raw_pre, (bytes, bytearray)):
                raw_pre = raw_pre.decode("utf-8")
            pre_pause_state = raw_pre

    if req.instruction and req.instruction.strip():
        from services.api_server.sag.approval import record_steering_instruction
        record_steering_instruction(
            redis_client=redis_client,
            incident_id=incident_id,
            instruction=req.instruction,
            actor=actor,
            channel=req.channel,
        )

    # If operator provided steering, always route to REMEDIATION to re-plan with guidance
    # If no steering was provided, resume back to the pre-pause state if supported
    trigger_event = "resume"
    if not (req.instruction and req.instruction.strip()) and pre_pause_state:
        target_map = {
            "EXECUTING": "resume_executing",
            "MONITORING": "resume_monitoring",
            "AWAITING_APPROVAL": "resume_approval",
            "TRIAGE": "resume_triage",
        }
        if pre_pause_state in target_map:
            trigger_event = target_map[pre_pause_state]

    machine = load_machine(incident_id, redis_client)
    try:
        meta = _attach_actor_audit(
            {"instruction": req.instruction, "channel": req.channel, "resumed_to": machine.state},
            actor_meta,
        )
        await machine.trigger(trigger_event, actor, meta)
        return {"status": "resumed", "incident_id": incident_id, "state": machine.state, "actor": actor}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/incidents/{incident_id}/action")
async def get_incident_action(incident_id: str, request: Request, action_id: Optional[str] = None):
    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(status_code=404, detail="No action record found")

    incident = get_incident(redis_client, incident_id)
    if incident:
        incident_id = incident.get("incident_id", incident_id)

    key = f"incident:{incident_id}:action:{action_id}" if action_id else f"incident:{incident_id}:action"
    raw = redis_client.get(key)
    if not raw:
        raise HTTPException(status_code=404, detail="No action record found")

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")

    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Corrupt action record: {exc}")

@router.post("/incidents/{incident_id}/approve")
async def approve_incident_action(incident_id: str, req: ApprovalDecisionRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    _verify_not_emergency_halt(redis_client)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    decision = req.decision.strip().lower()
    if decision not in ("approved", "denied", "steer"):
        raise HTTPException(status_code=400, detail="Decision must be 'approved', 'denied', or 'steer'")

    if decision == "steer" and (not req.instruction or not req.instruction.strip()):
        raise HTTPException(status_code=400, detail="instruction is required when decision is 'steer'")

    action_key = f"incident:{incident_id}:action:{req.action_id}"
    raw = None
    if redis_client is not None:
        raw = redis_client.get(action_key)

    if not raw:
        raise HTTPException(status_code=404, detail=f"Action proposal '{req.action_id}' not found")

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")

    try:
        record = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Corrupt action record in store: {exc}")

    current_status = record.get("status")
    non_overridable = {"steered", "timed_out", "expired"}
    # Deny wins permanently: a denied action cannot be approved, steered, or re-decided
    if (current_status == "denied") \
       or (current_status == "approved" and decision == "approved") \
       or (current_status in non_overridable and decision != "denied"):
        raise HTTPException(
            status_code=409,
            detail=f"Action '{req.action_id}' has already been decided: '{current_status}'",
        )

    machine = load_machine(incident_id, redis_client)
    if decision == "approved" and machine.state not in ("AWAITING_APPROVAL", "ESCALATED"):
        raise HTTPException(status_code=409, detail=f"Incident is in {machine.state}; cannot approve")

    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    record["status"] = "steered" if decision == "steer" else decision
    record["decided_by"] = actor
    record["decided_by_meta"] = actor_meta
    if actor_meta.get("key_id"):
        record["auth_key_id"] = actor_meta["key_id"]
    record["channel"] = req.channel
    record["decided_at"] = now_iso
    if req.reason:
        record["reason"] = req.reason
    if req.instruction:
        record["instruction"] = req.instruction

    if redis_client is not None:
        raw_to_save = json.dumps(record)
        redis_client.set(action_key, raw_to_save)
        redis_client.set(f"incident:{incident_id}:action", raw_to_save)
        if decision == "approved" and hasattr(redis_client, "sadd"):
            redis_client.sadd(f"incident:{incident_id}:approved_actions", req.action_id)
        elif decision == "denied" and hasattr(redis_client, "srem"):
            redis_client.srem(f"incident:{incident_id}:approved_actions", req.action_id)

    if decision == "approved":
        try:
            meta = _attach_actor_audit(
                {"action_id": req.action_id, "channel": req.channel, "reason": req.reason},
                actor_meta,
            )
            await machine.trigger("approved", actor, meta)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"Cannot transition to approved: {exc}")
        return {
            "status": "approved",
            "action_id": req.action_id,
            "incident_id": incident_id,
            "actor": actor,
            "message": f"Action '{req.action_id}' approved successfully.",
        }
    elif decision == "denied":
        from services.api_server.sag.approval import record_denial_notification
        record_denial_notification(
            redis_client=redis_client,
            incident_id=incident_id,
            action_id=req.action_id,
            actor=actor,
            channel=req.channel,
            reason=req.reason,
        )
        if req.channel != "telegram":
            from services.api_server.notifications import dispatch_notification_background
            cfg = getattr(request.app.state, "config", None)
            sys_cfg = getattr(cfg, "system", cfg)
            dispatch_notification_background(
                event="denial_notification",
                incident_id=incident_id,
                data={
                    "action_id": req.action_id,
                    "actor": actor,
                    "channel": req.channel,
                    "reason": req.reason,
                },
                config=sys_cfg,
            )
        try:
            meta = _attach_actor_audit(
                {"action_id": req.action_id, "channel": req.channel, "reason": req.reason},
                actor_meta,
            )
            await machine.trigger("denied", actor, meta)
        except ValueError:
            # approved/EXECUTING (or other state) – deny must still win
            try:
                late_meta = _attach_actor_audit(
                    {"reason": "denied_after_approval", "action_id": req.action_id, "channel": req.channel},
                    actor_meta,
                )
                await machine.trigger("escalate", actor, late_meta)
            except ValueError as exc:
                logger.warning("Failed to escalate incident %s after late denial: %s", incident_id, exc)
        return {
            "status": "denied",
            "action_id": req.action_id,
            "incident_id": incident_id,
            "actor": actor,
            "message": f"Action '{req.action_id}' denied.",
        }
    else:
        # Steer: record steering instruction and loop back to REMEDIATION
        from services.api_server.sag.approval import record_steering_instruction
        record_steering_instruction(
            redis_client=redis_client,
            incident_id=incident_id,
            instruction=req.instruction,
            actor=actor,
            channel=req.channel,
            action_id=req.action_id,
        )
        try:
            meta = _attach_actor_audit(
                {"action_id": req.action_id, "channel": req.channel, "instruction": req.instruction},
                actor_meta,
            )
            await machine.trigger("steer", actor, meta)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "status": "steered",
            "action_id": req.action_id,
            "incident_id": incident_id,
            "actor": actor,
            "instruction": req.instruction,
            "message": f"Action '{req.action_id}' superseded by steering instruction. Queued for re-planning.",
        }

@router.post("/incidents/{incident_id}/steer")
async def steer_incident(incident_id: str, req: SteerRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    _verify_not_emergency_halt(redis_client)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    if not req.instruction or not req.instruction.strip():
        raise HTTPException(status_code=400, detail="instruction cannot be empty")

    actor, actor_meta = resolve_request_actor(
        request,
        req.actor,
        req.channel,
        req.telegram_user_id,
        req.telegram_username,
        req.telegram_full_name,
    )

    from services.api_server.sag.approval import record_steering_instruction
    record_steering_instruction(
        redis_client=redis_client,
        incident_id=incident_id,
        instruction=req.instruction,
        actor=actor,
        channel=req.channel,
        action_id=req.action_id,
    )

    if req.action_id and redis_client:
        act_key = f"incident:{incident_id}:action:{req.action_id}"
        raw_act = redis_client.get(act_key)
        if raw_act:
            try:
                if isinstance(raw_act, (bytes, bytearray)):
                    raw_act = raw_act.decode("utf-8")
                act_dict = json.loads(raw_act) if isinstance(raw_act, str) else raw_act
                act_dict["status"] = "steered"
                act_dict["decided_by"] = actor
                act_dict["decided_by_meta"] = actor_meta
                if actor_meta.get("key_id"):
                    act_dict["auth_key_id"] = actor_meta["key_id"]
                act_dict["instruction"] = req.instruction
                redis_client.set(act_key, json.dumps(act_dict))
            except Exception as exc:
                logger.warning("Failed to update action status to steered for incident %s: %s", incident_id, exc, exc_info=True)

    machine = load_machine(incident_id, redis_client)
    try:
        meta = _attach_actor_audit(
            {"instruction": req.instruction, "channel": req.channel, "action_id": req.action_id},
            actor_meta,
        )
        await machine.trigger("steer", actor, meta)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "status": "steered",
        "incident_id": incident_id,
        "state": machine.state,
        "instruction": req.instruction,
        "channel": req.channel,
        "actor": actor,
        "message": "Steering instruction applied. Machine queued for remediation with guidance.",
    }

@router.get("/incidents/{incident_id}/steering")
async def get_incident_steering(incident_id: str, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    incident = get_incident(redis_client, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_id = incident.get("incident_id", incident_id)

    from services.api_server.sag.approval import get_steering_instructions
    return get_steering_instructions(redis_client, incident_id)


