"""Centralized interaction audit logging to Redis Stream and structured logger."""

import asyncio
from contextvars import ContextVar, Token
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("aegis.audit")

AUDIT_STREAM_KEY = "stream:audit:interactions"
AUDIT_STREAM_MAXLEN = 5000
DROPPED_ALERTS_STREAM_KEY = "stream:alerts:dropped"
DROPPED_ALERTS_MAXLEN = 2000

_SAFE_META_KEYS = {
    "credential_ref",
    "key_id",
    "api_key_env",
    "token_usage",
    "token_limit",
    "status_code",
}
_SECRET_SUBSTRINGS = ("api_key", "apikey", "password", "secret", "authorization", "bot_token")

_audit_context_var: ContextVar[Dict[str, Optional[str]]] = ContextVar(
    "aegis_audit_context",
    default={"incident_id": None, "caller": None},
)
_audit_redis_client: Optional[Any] = None


def set_audit_redis_client(redis_client: Any) -> None:
    """Register the process-level Redis client used by outbound audit hooks."""
    global _audit_redis_client
    _audit_redis_client = redis_client


def get_audit_redis_client() -> Optional[Any]:
    """Return the registered process-level Redis client for outbound audit logging."""
    return _audit_redis_client


def set_audit_context(
    incident_id: Optional[str] = None,
    caller: Optional[str] = None,
) -> Token[Dict[str, Optional[str]]]:
    """Bind current incident_id and caller service/agent into async task context."""
    current = dict(_audit_context_var.get())
    if incident_id is not None:
        current["incident_id"] = incident_id
    if caller is not None:
        current["caller"] = caller
    return _audit_context_var.set(current)


def get_audit_context() -> Dict[str, Optional[str]]:
    """Return the active async task audit context (incident_id, caller)."""
    return dict(_audit_context_var.get())


def reset_audit_context(token: Token[Dict[str, Optional[str]]]) -> None:
    """Restore previous async task audit context using a ContextVar token."""
    _audit_context_var.reset(token)


def sanitize_audit_details(data: Any) -> Any:
    """Recursively redact secret-bearing fields from audit metadata."""
    if isinstance(data, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in data.items():
            k_low = str(k).lower()
            is_secret_key = (
                k_low not in _SAFE_META_KEYS
                and (
                    k_low in ("token", "access_token", "refresh_token", "bearer", "credentials")
                    or k_low.endswith("_token")
                    or any(marker in k_low for marker in _SECRET_SUBSTRINGS)
                )
            )
            if is_secret_key:
                cleaned[k] = "***REDACTED***"
            else:
                cleaned[k] = sanitize_audit_details(v)
        return cleaned
    if isinstance(data, list):
        return [sanitize_audit_details(item) for item in data]
    return data


def record_interaction(
    redis_client: Optional[Any],
    *,
    direction: str,
    category: str,
    actor: str,
    target: str,
    action: str,
    external: bool,
    status: str = "ok",
    entity_type: Optional[str] = None,
    key_id: Optional[str] = None,
    credential_ref: Optional[str] = None,
    incident_id: Optional[str] = None,
    latency_ms: Optional[float] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Record an inbound or outbound interaction to logs and Redis Stream 'stream:audit:interactions'."""
    now_iso = datetime.now(timezone.utc).isoformat()
    safe_details = sanitize_audit_details(details or {})

    logger.info(
        "[AUDIT] dir=%s cat=%s actor=%s type=%s key=%s target=%s action=%s ext=%s status=%s inc=%s",
        direction,
        category,
        actor,
        entity_type or "",
        key_id or credential_ref or "",
        target,
        action,
        external,
        status,
        incident_id or "",
    )

    if redis_client is None or not hasattr(redis_client, "xadd"):
        return None

    fields: Dict[str, str] = {
        "timestamp": now_iso,
        "direction": str(direction),
        "category": str(category),
        "actor": str(actor),
        "entity_type": str(entity_type or ""),
        "key_id": str(key_id or ""),
        "credential_ref": str(credential_ref or ""),
        "target": str(target),
        "action": str(action),
        "external": "true" if external else "false",
        "status": str(status),
        "incident_id": str(incident_id or ""),
        "latency_ms": f"{latency_ms:.2f}" if latency_ms is not None else "",
        "details": json.dumps(safe_details, default=str),
    }

    try:
        try:
            res = redis_client.xadd(
                AUDIT_STREAM_KEY,
                fields,
                maxlen=AUDIT_STREAM_MAXLEN,
                approximate=True,
            )
        except TypeError:
            res = redis_client.xadd(AUDIT_STREAM_KEY, fields)
        if isinstance(res, (bytes, bytearray)):
            return res.decode("utf-8")
        return str(res) if res is not None else None
    except Exception as exc:
        logger.debug("Failed writing interaction audit entry: %s", exc)
        return None


async def record_interaction_async(
    redis_client: Optional[Any],
    **kwargs: Any,
) -> Optional[str]:
    """Async-safe wrapper around record_interaction using asyncio.to_thread."""
    if redis_client is None or not hasattr(redis_client, "xadd"):
        return record_interaction(None, **kwargs)
    return await asyncio.to_thread(record_interaction, redis_client, **kwargs)


def _decode_field(val: Any) -> str:
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return str(val) if val is not None else ""


def list_interactions(
    redis_client: Optional[Any],
    limit: int = 100,
    direction: Optional[str] = None,
    external_only: bool = False,
    incident_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query recent interaction audit records from Redis Stream newest-first."""
    if redis_client is None:
        return []
    safe_limit = max(1, min(int(limit or 100), 500))
    fetch_count = max(safe_limit * 4, 200)

    raw_items: List[Any] = []
    try:
        if hasattr(redis_client, "xrevrange"):
            raw_items = redis_client.xrevrange(AUDIT_STREAM_KEY, "+", "-", count=fetch_count) or []
        elif hasattr(redis_client, "xrange"):
            items = redis_client.xrange(AUDIT_STREAM_KEY, "-", "+", count=fetch_count) or []
            raw_items = list(reversed(items))
    except Exception as exc:
        logger.warning("Failed to read audit interactions from Redis: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        msg_id, raw_fields = item
        if not isinstance(raw_fields, dict):
            continue
        decoded = {_decode_field(k): _decode_field(v) for k, v in raw_fields.items()}
        is_ext = decoded.get("external", "").lower() == "true"

        if direction and decoded.get("direction") != direction:
            continue
        if external_only and not is_ext:
            continue
        if incident_id and decoded.get("incident_id") != incident_id:
            continue

        parsed_details: Any = {}
        if decoded.get("details"):
            try:
                parsed_details = json.loads(decoded["details"])
            except Exception:
                parsed_details = {"raw": decoded["details"]}

        latency_val: Optional[float] = None
        if decoded.get("latency_ms"):
            try:
                latency_val = float(decoded["latency_ms"])
            except ValueError:
                latency_val = None

        results.append(
            {
                "id": _decode_field(msg_id),
                "timestamp": decoded.get("timestamp", ""),
                "direction": decoded.get("direction", ""),
                "category": decoded.get("category", ""),
                "actor": decoded.get("actor", ""),
                "entity_type": decoded.get("entity_type", ""),
                "key_id": decoded.get("key_id", ""),
                "credential_ref": decoded.get("credential_ref", ""),
                "target": decoded.get("target", ""),
                "action": decoded.get("action", ""),
                "external": is_ext,
                "status": decoded.get("status", ""),
                "incident_id": decoded.get("incident_id", ""),
                "latency_ms": latency_val,
                "details": parsed_details,
            }
        )
        if len(results) >= safe_limit:
            break

    return results


def record_dropped_alert(
    redis_client: Optional[Any],
    *,
    alertname: str,
    service: str,
    namespace: str,
    allowed_namespaces: List[str],
    fingerprint: str = "",
    ingested_by: str = "",
    key_id: str = "",
    reason: str = "namespace_not_monitored",
) -> Optional[str]:
    """Record an out-of-scope dropped alert to stream:alerts:dropped and stream:audit:interactions."""
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.warning(
        "[ALERT DROPPED] alert=%s service=%s namespace=%s allowed=%s reason=%s actor=%s",
        alertname,
        service,
        namespace,
        allowed_namespaces,
        reason,
        ingested_by or "unknown",
    )

    msg_id: Optional[str] = None
    if redis_client is not None and hasattr(redis_client, "xadd"):
        fields = {
            "timestamp": now_iso,
            "alertname": str(alertname),
            "service": str(service),
            "namespace": str(namespace),
            "allowed_namespaces": json.dumps(list(allowed_namespaces)),
            "fingerprint": str(fingerprint or ""),
            "ingested_by": str(ingested_by or ""),
            "key_id": str(key_id or ""),
            "reason": str(reason),
        }
        try:
            try:
                res = redis_client.xadd(
                    DROPPED_ALERTS_STREAM_KEY,
                    fields,
                    maxlen=DROPPED_ALERTS_MAXLEN,
                    approximate=True,
                )
            except TypeError:
                res = redis_client.xadd(DROPPED_ALERTS_STREAM_KEY, fields)
            msg_id = _decode_field(res) if res is not None else None
        except Exception as exc:
            logger.debug("Failed writing dropped alert entry: %s", exc)

    record_interaction(
        redis_client,
        direction="inbound",
        category="alert_dropped_namespace",
        actor=ingested_by or "alert_ingest",
        entity_type="webhook" if ingested_by else "system",
        key_id=key_id or None,
        target=f"namespace:{namespace}",
        action=f"drop_alert:{alertname}",
        external=True,
        status="dropped",
        details={
            "service": service,
            "namespace": namespace,
            "allowed_namespaces": list(allowed_namespaces),
            "fingerprint": fingerprint,
            "reason": reason,
        },
    )
    return msg_id


async def record_dropped_alert_async(
    redis_client: Optional[Any],
    **kwargs: Any,
) -> Optional[str]:
    """Async-safe wrapper around record_dropped_alert using asyncio.to_thread."""
    if redis_client is None or not hasattr(redis_client, "xadd"):
        return record_dropped_alert(None, **kwargs)
    return await asyncio.to_thread(record_dropped_alert, redis_client, **kwargs)


def list_dropped_alerts(
    redis_client: Optional[Any],
    limit: int = 100,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query recent dropped out-of-scope alert records newest-first."""
    if redis_client is None:
        return []
    safe_limit = max(1, min(int(limit or 100), 500))
    fetch_count = max(safe_limit * 4, 200)

    raw_items: List[Any] = []
    try:
        if hasattr(redis_client, "xrevrange"):
            raw_items = redis_client.xrevrange(DROPPED_ALERTS_STREAM_KEY, "+", "-", count=fetch_count) or []
        elif hasattr(redis_client, "xrange"):
            items = redis_client.xrange(DROPPED_ALERTS_STREAM_KEY, "-", "+", count=fetch_count) or []
            raw_items = list(reversed(items))
    except Exception as exc:
        logger.warning("Failed reading dropped alerts stream: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        msg_id, raw_fields = item
        if not isinstance(raw_fields, dict):
            continue
        decoded = {_decode_field(k): _decode_field(v) for k, v in raw_fields.items()}
        if namespace and decoded.get("namespace", "").lower() != namespace.strip().lower():
            continue
        allowed_ns: List[str] = []
        if decoded.get("allowed_namespaces"):
            try:
                allowed_ns = json.loads(decoded["allowed_namespaces"])
            except Exception:
                allowed_ns = []
        results.append(
            {
                "id": _decode_field(msg_id),
                "timestamp": decoded.get("timestamp", ""),
                "alertname": decoded.get("alertname", ""),
                "service": decoded.get("service", ""),
                "namespace": decoded.get("namespace", ""),
                "allowed_namespaces": allowed_ns,
                "fingerprint": decoded.get("fingerprint", ""),
                "ingested_by": decoded.get("ingested_by", ""),
                "key_id": decoded.get("key_id", ""),
                "reason": decoded.get("reason", ""),
            }
        )
        if len(results) >= safe_limit:
            break

    return results

