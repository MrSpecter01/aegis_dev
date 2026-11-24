import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from shared.audit.tracker import list_interactions
from services.api_server.auth_context import resolve_request_actor

router = APIRouter()

# Metrics definition
aegis_incident_state_transitions_total = Counter(
    'aegis_incident_state_transitions_total',
    'Total number of state transitions',
    ['from_state', 'to_state']
)

aegis_active_incidents_total = Gauge(
    'aegis_active_incidents_total',
    'Number of active incidents by state',
    ['state']
)

aegis_api_request_duration_seconds = Histogram(
    'aegis_api_request_duration_seconds',
    'API request latency'
)

@router.get("/health")
async def health(request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    redis_status = "error"
    if redis_client:
        try:
            if redis_client.ping():
                redis_status = "ok"
        except Exception as exc:
            logging.getLogger(__name__).warning("Redis health check ping failed: %s", exc)
            
    return {"status": "ok", "redis": redis_status}

class SystemPauseRequest(BaseModel):
    reason: str
    actor: str = "operator"

class SystemResumeRequest(BaseModel):
    actor: str = "operator"

@router.get("/auth/me")
async def get_current_principal(request: Request):
    """Return the authenticated caller principal resolved by APIKeyMiddleware."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return principal.model_dump()

@router.get("/audit/interactions")
async def get_audit_interactions(
    request: Request,
    limit: int = 100,
    direction: Optional[str] = None,
    external_only: bool = False,
    incident_id: Optional[str] = None,
):
    """List recent inbound and outbound entity interaction audit entries."""
    redis_client = getattr(request.app.state, "redis", None)
    items = await asyncio.to_thread(
        list_interactions,
        redis_client,
        limit=limit,
        direction=direction,
        external_only=external_only,
        incident_id=incident_id,
    )
    return {"count": len(items), "items": items}

@router.get("/system/status")
async def get_system_status(request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    paused = False
    details = {}
    if redis_client:
        raw = redis_client.get("system:status:paused")
        if raw:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            try:
                details = json.loads(raw)
                paused = bool(details.get("paused", False))
            except Exception:
                paused = False
    return {
        "status": "paused" if paused else "operational",
        "paused": paused,
        "details": details,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }

@router.post("/system/pause")
async def pause_system(req: SystemPauseRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    actor, actor_meta = resolve_request_actor(request, req.actor, "dashboard")
    payload = {
        "paused": True,
        "actor": actor,
        "auth_key_id": actor_meta.get("key_id"),
        "reason": req.reason,
        "paused_at": datetime.now(timezone.utc).isoformat(),
    }
    if redis_client:
        redis_client.set("system:status:paused", json.dumps(payload))
    return {"status": "paused", **payload}

@router.post("/system/resume")
async def resume_system(req: SystemResumeRequest, request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    actor, actor_meta = resolve_request_actor(request, req.actor, "dashboard")
    payload = {
        "paused": False,
        "actor": actor,
        "auth_key_id": actor_meta.get("key_id"),
        "resumed_at": datetime.now(timezone.utc).isoformat(),
    }
    if redis_client:
        redis_client.set("system:status:paused", json.dumps(payload))
    return {"status": "operational", **payload}

@router.post("/system/demo-incident")
async def seed_demo_incident(request: Request):
    """Seed a realistic interactive incident in AWAITING_APPROVAL state with pending actions and ledger."""
    from datetime import timedelta
    from shared.models.enums import IncidentState
    from shared.models.ledger import LedgerEntry
    from shared.ledger.writer import append_ledger_entry

    redis_client = getattr(request.app.state, "redis", None)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    seq = 1
    if redis_client:
        try:
            seq = redis_client.incr(f"incident:seq:{today}")
        except Exception:
            pass

    inc_id = f"inc-{today}-{seq:04d}"
    now_iso = datetime.now(timezone.utc).isoformat()
    exp_iso = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    from shared.config.loader import ConfigManager
    from services.api_server.topology.store import TopologyStore

    sag_cfg = ConfigManager().sag
    allowed_ns = sag_cfg.tools.get("restart_pod").allowed_namespaces if ("restart_pod" in sag_cfg.tools and sag_cfg.tools["restart_pod"].allowed_namespaces) else []
    target_ns = allowed_ns[0] if allowed_ns else "default"
    target_svc = "demo-service"

    if redis_client:
        try:
            nodes = TopologyStore(redis_client).get_all_nodes()
            if nodes:
                matching_nodes = [n for n in nodes if getattr(n, "namespace", "") == target_ns]
                chosen_node = matching_nodes[0] if matching_nodes else nodes[0]
                target_svc = chosen_node.service_name or chosen_node.node_id
                target_ns = getattr(chosen_node, "namespace", target_ns) or target_ns
        except Exception:
            pass

    incident_data = {
        "incident_id": inc_id,
        "state": "AWAITING_APPROVAL",
        "visited_states": ["NEW", "TRIAGE", "MERGING", "REMEDIATION", "AWAITING_APPROVAL"],
        "last_transition": {
            "from": "REMEDIATION",
            "to": "AWAITING_APPROVAL",
            "timestamp": now_iso,
        },
        "severity": "P1",
        "affected_service": target_svc,
        "affected_namespace": target_ns,
        "created_at": now_iso,
        "updated_at": now_iso,
        "retry_count": 0,
    }

    context_data = {
        "primary_hypothesis": f"Resource exhaustion in {target_svc} under elevated traffic; pod restarting frequently with OOMKilled.",
        "confidence": 0.94,
        "corroboration_score": 3,
        "ranked_evidence": [
            "container_memory_working_set_bytes exceeded 90% quota for > 2m",
            "OOMKilled exit code 137 logged in k8s events",
            f"P99 latency spike to 840ms on /{target_svc} endpoint across all pods",
        ],
        "affected_services": [target_svc],
    }

    action_record = {
        "action_id": "act-demo-001",
        "status": "pending",
        "incident_id": inc_id,
        "proposal": {
            "rationale": f"Restart {target_svc} pod to release leaked memory buffer and restore SLO latency within bounds.",
            "confidence": 0.92,
            "reversible": True,
            "actions": [
                {
                    "tool_name": "restart_pod",
                    "target_resource": f"{target_svc}.{target_ns}",
                    "parameters": {
                        "namespace": target_ns,
                        "pod_name": f"{target_svc}-789cfd-x89p",
                    },
                }
            ],
        },
        "created_at": now_iso,
        "expires_at": exp_iso,
    }

    if redis_client:
        redis_client.set(f"incident:{inc_id}:state", json.dumps(incident_data))
        redis_client.set(f"incident:{inc_id}:context", json.dumps(context_data))
        redis_client.set(f"incident:{inc_id}:action", json.dumps(action_record))
        redis_client.set(f"incident:{inc_id}:action:act-demo-001", json.dumps(action_record))

        # Add initial ledger audit trail
        append_ledger_entry(
            redis_client,
            inc_id,
            LedgerEntry(
                entry_id=f"entry-{inc_id}-001",
                incident_id=inc_id,
                timestamp=datetime.now(timezone.utc),
                state=IncidentState.NEW,
                event_type="IncidentDetected",
                source="alertmanager",
                details={"alert": "PodMemoryHigh", "severity": "P1"},
            ),
        )
        append_ledger_entry(
            redis_client,
            inc_id,
            LedgerEntry(
                entry_id=f"entry-{inc_id}-002",
                incident_id=inc_id,
                timestamp=datetime.now(timezone.utc),
                state=IncidentState.TRIAGE,
                event_type="InvestigatorFlowInitiated",
                source="phase1-worker",
                details={"agents": ["infrastructure", "logs_traces", "change_config"]},
            ),
        )
        append_ledger_entry(
            redis_client,
            inc_id,
            LedgerEntry(
                entry_id=f"entry-{inc_id}-003",
                incident_id=inc_id,
                timestamp=datetime.now(timezone.utc),
                state=IncidentState.AWAITING_APPROVAL,
                event_type="EvidenceCorroborated",
                source="phase1-worker",
                details={"confidence": 0.94, "corroboration_score": 3},
            ),
        )
        # Dispatch approval notification to Telegram so interactive buttons appear
        from services.api_server.notifications import dispatch_notification_background
        cfg = getattr(request.app.state, "config", None)
        sys_cfg = getattr(cfg, "system", cfg)
        dispatch_notification_background(
            event="approval_request",
            incident_id=inc_id,
            data={"incident": incident_data, "action": action_record},
            config=sys_cfg,
        )

    return {"status": "created", "incident_id": inc_id, "data": incident_data}

@router.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return generate_latest()
