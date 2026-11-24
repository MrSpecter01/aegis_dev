"""Canonical SHA-256 digest versioning and operator-gated pending drift helpers."""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("aegis.versioning")

TOPOLOGY_META_KEY = "topology:meta"
RUNBOOKS_META_KEY = "runbooks:index:meta"
DRIFT_PENDING_PREFIX = "drift:pending:"


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_topology_digest(nodes: List[Any], edges: List[Any]) -> str:
    """Compute a deterministic SHA-256 hex digest for a sorted topology graph."""
    norm_nodes: List[Dict[str, Any]] = []
    for n in nodes or []:
        d = n.model_dump() if hasattr(n, "model_dump") else dict(n)
        if isinstance(d.get("dependencies"), list):
            d["dependencies"] = sorted(str(dep) for dep in d["dependencies"])
        if isinstance(d.get("ports"), list):
            d["ports"] = sorted(d["ports"])
        norm_nodes.append(d)
    norm_nodes.sort(key=lambda item: str(item.get("id", "")))

    norm_edges: List[Dict[str, Any]] = []
    for e in edges or []:
        d = e.model_dump(by_alias=True) if hasattr(e, "model_dump") else dict(e)
        norm_edges.append(d)
    norm_edges.sort(
        key=lambda item: (
            str(item.get("from") or item.get("from_node") or ""),
            str(item.get("to") or item.get("to_node") or ""),
            str(item.get("port") or ""),
        )
    )

    payload = _canonical_json({"nodes": norm_nodes, "edges": norm_edges})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_runbooks_digest(entries: List[Dict[str, Any]]) -> str:
    """Compute a deterministic SHA-256 hex digest for a compiled runbooks index."""
    norm_entries: List[Dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        if isinstance(item.get("allowed_actions"), list):
            item["allowed_actions"] = sorted(str(a) for a in item["allowed_actions"])
        norm_entries.append(item)
    norm_entries.sort(key=lambda item: str(item.get("id", "")))
    return hashlib.sha256(_canonical_json(norm_entries).encode("utf-8")).hexdigest()


def _decode_json_dict(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def read_meta(redis_client: Optional[Any], key: str) -> Dict[str, Any]:
    """Read version/SHA metadata dict from Redis."""
    if redis_client is None or not hasattr(redis_client, "get"):
        return {}
    try:
        return _decode_json_dict(redis_client.get(key))
    except Exception as exc:
        logger.debug("Failed reading meta key %s: %s", key, exc)
        return {}


def write_meta(redis_client: Optional[Any], key: str, meta: Dict[str, Any]) -> None:
    """Persist version/SHA metadata dict to Redis."""
    if redis_client is None or not hasattr(redis_client, "set"):
        return
    try:
        redis_client.set(key, json.dumps(meta, default=str))
    except Exception as exc:
        logger.warning("Failed writing meta key %s: %s", key, exc)


def get_pending_drift(redis_client: Optional[Any], target: str) -> Optional[Dict[str, Any]]:
    """Fetch a pending drift record for 'topology' or 'runbooks', or None."""
    if redis_client is None or not hasattr(redis_client, "get"):
        return None
    try:
        data = _decode_json_dict(redis_client.get(f"{DRIFT_PENDING_PREFIX}{target}"))
        return data if data else None
    except Exception as exc:
        logger.debug("Failed reading pending drift %s: %s", target, exc)
        return None


def set_pending_drift(redis_client: Optional[Any], target: str, record: Dict[str, Any]) -> None:
    """Persist an awaiting-operator-approval drift record in Redis."""
    if redis_client is None or not hasattr(redis_client, "set"):
        return
    try:
        redis_client.set(f"{DRIFT_PENDING_PREFIX}{target}", json.dumps(record, default=str))
    except Exception as exc:
        logger.warning("Failed writing pending drift %s: %s", target, exc)


def clear_pending_drift(redis_client: Optional[Any], target: str) -> None:
    """Delete a pending drift record after approval, rejection, or sync."""
    if redis_client is None:
        return
    key = f"{DRIFT_PENDING_PREFIX}{target}"
    try:
        if hasattr(redis_client, "delete"):
            redis_client.delete(key)
        elif hasattr(redis_client, "set"):
            redis_client.set(key, "")
    except Exception as exc:
        logger.debug("Failed clearing pending drift %s: %s", target, exc)

