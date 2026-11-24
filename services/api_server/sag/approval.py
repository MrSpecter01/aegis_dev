import asyncio
from datetime import datetime, timezone, timedelta
import json
import uuid
from typing import Any, Dict, List, Optional

from shared.config.loader import ConfigManager
from shared.models.actions import ActionProposal, ApprovalRequest


async def emit_approval_request(
    redis_client: Any,
    incident_id: str,
    action_proposal: ActionProposal,
    severity: str = "P2",
    timeout_seconds: Optional[int] = None,
    action_id: Optional[str] = None,
    schedule_timeout: bool = False,
    auto_approve: bool = False,
) -> ApprovalRequest:
    """
    Emit an approval request for an ActionProposal.
    
    1. Writes action record to Redis with configurable TTL per severity.
       If auto_approve is True, status is set to 'approved' and decided_by to 'policy'.
    2. Exposes pending action to dashboard and notification channels.
    3. If schedule_timeout is True and not auto_approve, starts an asynchronous timeout task.
    """
    act_id = action_id or f"act-{incident_id}-{uuid.uuid4().hex[:8]}"

    retention_seconds = 3600
    if timeout_seconds is None:
        try:
            cfg = ConfigManager().sag
            timeout_seconds = cfg.get_timeout_seconds(severity)
            retention_seconds = getattr(cfg, "record_retention_seconds", 3600)
        except Exception:
            timeout_seconds = 900  # Default P2: 15 min
    else:
        try:
            cfg = ConfigManager().sag
            retention_seconds = getattr(cfg, "record_retention_seconds", 3600)
        except Exception:
            retention_seconds = 3600

    ttl_seconds = timeout_seconds + retention_seconds
    now_iso = datetime.now(timezone.utc).isoformat()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

    status = "approved" if auto_approve else "pending"
    record: Dict[str, Any] = {
        "action_id": act_id,
        "incident_id": incident_id,
        "status": status,
        "severity": severity,
        "proposal": action_proposal.model_dump(mode="json"),
        "runbook_citations": [c.model_dump(mode="json") for c in action_proposal.runbook_citations],
        "expires_at": expires_at.isoformat(),
        "timeout_seconds": timeout_seconds,
        "created_at": now_iso,
    }
    if auto_approve:
        record["decided_by"] = "policy"
        record["channel"] = "pre_approved"
        record["decided_at"] = now_iso

    raw_json = json.dumps(record)

    if redis_client is not None:
        # Write primary action record
        primary_key = f"incident:{incident_id}:action:{act_id}"
        alias_key = f"incident:{incident_id}:action"
        pending_list_key = f"incident:{incident_id}:pending_actions"

        # Check if redis_client is async or sync
        if asyncio.iscoroutinefunction(getattr(redis_client, "set", None)):
            await redis_client.set(primary_key, raw_json, ex=ttl_seconds)
            await redis_client.set(alias_key, raw_json, ex=ttl_seconds)
            if hasattr(redis_client, "rpush"):
                await redis_client.rpush(pending_list_key, act_id)
        else:
            redis_client.set(primary_key, raw_json, ex=ttl_seconds)
            redis_client.set(alias_key, raw_json, ex=ttl_seconds)
            if hasattr(redis_client, "rpush"):
                redis_client.rpush(pending_list_key, act_id)

    approval_request = ApprovalRequest(
        request_id=act_id,
        incident_id=incident_id,
        proposal=action_proposal,
        channel_notified=["dashboard", "telegram"],
        expires_at=expires_at,
    )

    if not auto_approve and schedule_timeout and timeout_seconds > 0:
        asyncio.create_task(
            handle_approval_timeout(redis_client, incident_id, act_id, timeout_seconds)
        )

    return approval_request


async def handle_approval_timeout(
    redis_client: Any,
    incident_id: str,
    action_id: str,
    timeout_seconds: float,
) -> None:
    """Coroutine that auto-escalates an incident if an action times out without approval."""
    import logging
    logger = logging.getLogger("aegis.sag.approval")

    await asyncio.sleep(timeout_seconds)

    if redis_client is None:
        return

    primary_key = f"incident:{incident_id}:action:{action_id}"
    raw = None
    if asyncio.iscoroutinefunction(getattr(redis_client, "get", None)):
        raw = await redis_client.get(primary_key)
    else:
        raw = redis_client.get(primary_key)

    if not raw:
        logger.warning("approval record %s missing at timeout; escalating", action_id)
        try:
            from services.api_server.state_machine import load_machine
            machine = load_machine(incident_id, redis_client)
            await machine.trigger(
                "escalate",
                actor="sag_approval_timer",
                metadata={"reason": "approval_record_missing", "action_id": action_id},
            )
        except Exception as exc:
            logger.warning("timeout escalate skipped for %s: %s", incident_id, exc)
        return

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")

    try:
        record = json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to decode approval record for %s: %s", action_id, exc, exc_info=True)
        return

    # If action is still pending, auto-escalate (hard rule: never auto-approve)
    if record.get("status") == "pending":
        record["status"] = "timed_out"
        record["timeout_at"] = datetime.now(timezone.utc).isoformat()
        updated_json = json.dumps(record)

        if asyncio.iscoroutinefunction(getattr(redis_client, "set", None)):
            await redis_client.set(primary_key, updated_json)
        else:
            redis_client.set(primary_key, updated_json)

        # Trigger state machine escalate transition
        try:
            from services.api_server.state_machine import load_machine
            machine = load_machine(incident_id, redis_client)
            await machine.trigger(
                "escalate",
                actor="sag_approval_timer",
                metadata={"reason": "approval_timeout", "action_id": action_id},
            )
        except Exception as exc:
            logger.warning("timeout escalate skipped for %s: %s", incident_id, exc)


def record_denial_notification(
    redis_client: Any,
    incident_id: str,
    action_id: str,
    actor: str,
    channel: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Record cross-channel denial notification payload in Redis."""
    target_channel = "dashboard" if channel == "telegram" else "telegram"
    notification = {
        "action_id": action_id,
        "incident_id": incident_id,
        "actor": actor,
        "denied_via": channel,
        "target_channel": target_channel,
        "message": f"Action {action_id} was denied by {actor} via {channel}",
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if redis_client is not None:
        key = f"incident:{incident_id}:denial_notification"
        raw = json.dumps(notification)
        if hasattr(redis_client, "set"):
            redis_client.set(key, raw)

    return notification


def record_steering_instruction(
    redis_client: Any,
    incident_id: str,
    instruction: str,
    actor: str,
    channel: str,
    action_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record operator steering instructions in Redis.
    Persists to list 'incident:{id}:steering' and updates 'incident:{id}:context'
    so agent retrieves guidance upon starting or resuming.
    """
    instruction_id = f"str-{uuid.uuid4().hex[:8]}"
    record = {
        "instruction_id": instruction_id,
        "incident_id": incident_id,
        "action_id": action_id,
        "instruction": instruction,
        "actor": actor,
        "channel": channel,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    raw = json.dumps(record)

    if redis_client is not None:
        key = f"incident:{incident_id}:steering"
        if hasattr(redis_client, "set"):
            redis_client.set(key, raw)

        # Also update incident context
        ctx_key = f"incident:{incident_id}:context"
        ctx_raw = redis_client.get(ctx_key) if hasattr(redis_client, "get") else None
        context = {}
        if ctx_raw:
            try:
                if isinstance(ctx_raw, (bytes, bytearray)):
                    ctx_raw = ctx_raw.decode("utf-8")
                context = json.loads(ctx_raw) if isinstance(ctx_raw, str) else ctx_raw
            except Exception:
                context = {}

        steering_list = context.get("steering_instructions", [])
        steering_list.append(record)
        context["steering_instructions"] = steering_list
        context["latest_steering"] = instruction
        if hasattr(redis_client, "set"):
            redis_client.set(ctx_key, json.dumps(context))

    return record


def get_steering_instructions(
    redis_client: Any,
    incident_id: str,
) -> List[Dict[str, Any]]:
    """Retrieve all steering instructions recorded for an incident."""
    if redis_client is None:
        return []

    key = f"incident:{incident_id}:steering"
    instructions = []
    if hasattr(redis_client, "get"):
        item = redis_client.get(key)
        if item:
            try:
                if isinstance(item, (bytes, bytearray)):
                    item = item.decode("utf-8")
                instructions.append(json.loads(item) if isinstance(item, str) else item)
            except Exception as exc:
                logger.warning("Failed to parse steering item for incident %s: %s", incident_id, exc)
    return instructions


async def rearm_pending_approval_timers(redis_client: Any) -> int:
    """
    Startup sweep: iterate pending action lists in Redis, re-arm timers for
    still-pending records or escalate already-expired ones.
    """
    import logging
    logger = logging.getLogger("aegis.sag.approval")
    if redis_client is None:
        return 0

    rearmed_count = 0
    try:
        keys = []
        if hasattr(redis_client, "keys"):
            if asyncio.iscoroutinefunction(redis_client.keys):
                keys = await redis_client.keys("incident:*:pending_actions")
            else:
                keys = redis_client.keys("incident:*:pending_actions")

        for k in keys:
            if isinstance(k, (bytes, bytearray)):
                k = k.decode("utf-8")
            parts = k.split(":")
            if len(parts) < 3:
                continue
            incident_id = parts[1]

            action_ids = []
            if hasattr(redis_client, "lrange"):
                if asyncio.iscoroutinefunction(redis_client.lrange):
                    action_ids = await redis_client.lrange(k, 0, -1)
                else:
                    action_ids = redis_client.lrange(k, 0, -1)

            for act_id in action_ids:
                if isinstance(act_id, (bytes, bytearray)):
                    act_id = act_id.decode("utf-8")
                primary_key = f"incident:{incident_id}:action:{act_id}"
                raw = None
                if hasattr(redis_client, "get"):
                    if asyncio.iscoroutinefunction(redis_client.get):
                        raw = await redis_client.get(primary_key)
                    else:
                        raw = redis_client.get(primary_key)
                if not raw:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8")
                try:
                    record = json.loads(raw)
                except Exception as exc:
                    logger.warning("Failed to decode pending action record %s: %s", act_id, exc, exc_info=True)
                    continue

                if record.get("status") == "pending":
                    now = datetime.now(timezone.utc)
                    expires_at_str = record.get("expires_at")
                    try:
                        expires_at = datetime.fromisoformat(expires_at_str) if expires_at_str else None
                    except Exception as exc:
                        logger.warning("Failed to parse expires_at '%s' for action %s: %s", expires_at_str, act_id, exc)
                        expires_at = None

                    if expires_at and expires_at <= now:
                        # Already expired while offline -> escalate now
                        record["status"] = "timed_out"
                        record["timeout_at"] = now.isoformat()
                        updated_json = json.dumps(record)
                        if asyncio.iscoroutinefunction(getattr(redis_client, "set", None)):
                            await redis_client.set(primary_key, updated_json)
                        else:
                            redis_client.set(primary_key, updated_json)
                        try:
                            from services.api_server.state_machine import load_machine
                            machine = load_machine(incident_id, redis_client)
                            await machine.trigger(
                                "escalate",
                                actor="startup_sweep",
                                metadata={"reason": "approval_timeout_on_startup", "action_id": act_id},
                            )
                        except Exception as exc:
                            logger.warning("Startup sweep escalate skipped for %s: %s", incident_id, exc)
                    else:
                        remaining_seconds = (expires_at - now).total_seconds() if expires_at else 0.0
                        asyncio.create_task(
                            handle_approval_timeout(redis_client, incident_id, act_id, max(0.0, remaining_seconds))
                        )
                        rearmed_count += 1
    except Exception as exc:
        logger.warning("Failed during pending action sweep: %s", exc)

    return rearmed_count


