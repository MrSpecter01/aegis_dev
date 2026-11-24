import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import redis

from shared.config.loader import ConfigManager
from shared.config.schemas import LedgerConfig
from shared.ledger.writer import dump_ledger_to_disk, get_ledger_entries
from shared.models.alerts import AlertGroup
from shared.models.enums import IncidentState

logger = logging.getLogger("aegis.api_server.incident_store")

_INCIDENT_TTL_SUFFIXES = (
    "state",
    "ledger",
    "context",
    "flags",
    "alert_group",
    "action",
    "proposal",
    "prior_attempts",
    "mutation_delta",
    "steering",
    "dropped",
    "pre_pause_state",
    "approved_actions",
    "dumped_at",
)

_SNAPSHOT_KEY_SUFFIXES = (
    "state",
    "context",
    "flags",
    "alert_group",
    "action",
    "proposal",
    "prior_attempts",
    "mutation_delta",
    "steering",
)

def generate_incident_id(redis_client: Optional[redis.Redis]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    seq = 1
    if redis_client and isinstance(redis_client, redis.Redis):
        seq = redis_client.incr(f"incident:seq:{today}")
    return f"inc-{today}-{seq:04d}"

def create_incident(redis_client: Optional[redis.Redis], incident_id: str, alert_group: AlertGroup) -> dict:
    data = {
        "incident_id": incident_id,
        "state": IncidentState.NEW.value,
        "visited_states": [IncidentState.NEW.value],
        "last_transition": None,
        "severity": alert_group.severity.value,
        "affected_service": alert_group.affected_service,
        "affected_namespace": alert_group.affected_namespace,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "retry_count": 0
    }
    if redis_client and isinstance(redis_client, redis.Redis):
        redis_client.set(f"incident:{incident_id}:state", json.dumps(data))
    return data

def _normalize_incident_id(incident_id: str) -> str:
    """Normalize incident ID with flexible sequence padding (e.g. inc-20261021-004 -> inc-20261021-0004)."""
    parts = incident_id.split("-")
    if len(parts) == 3 and parts[0] == "inc" and len(parts[1]) == 8 and parts[2].isdigit():
        return f"inc-{parts[1]}-{int(parts[2]):04d}"
    return incident_id


def check_report_exists(incident_id: str) -> bool:
    from pathlib import Path
    return Path(f"/data/incidents/{incident_id}/report.md").is_file() or (Path.cwd() / "data" / "incidents" / incident_id / "report.md").is_file()


def get_incident(redis_client: Optional[redis.Redis], incident_id: str) -> Optional[dict]:
    if not redis_client or not isinstance(redis_client, redis.Redis):
        return None
    raw = redis_client.get(f"incident:{incident_id}:state")
    if not raw:
        norm_id = _normalize_incident_id(incident_id)
        if norm_id != incident_id:
            raw = redis_client.get(f"incident:{norm_id}:state")
    if raw and isinstance(raw, (str, bytes, bytearray)):
        try:
            data = json.loads(raw)
            inc_id = data.get("incident_id", incident_id)
            data["has_report"] = check_report_exists(inc_id)
            return data
        except Exception:
            return None
    return None

def list_incidents(redis_client: Optional[redis.Redis], state_filter: Optional[str] = None, severity_filter: Optional[str] = None) -> List[dict]:
    if not redis_client or not isinstance(redis_client, redis.Redis):
        return []
    
    incidents = []
    for key in redis_client.scan_iter("incident:inc-*:state"):
        raw = redis_client.get(key)
        if raw:
            data = json.loads(raw)
            if state_filter and data.get("state") != state_filter:
                continue
            if severity_filter and data.get("severity") != severity_filter:
                continue
            inc_id = data.get("incident_id")
            if inc_id:
                data["has_report"] = check_report_exists(inc_id)
            incidents.append(data)
    return incidents

def _decode_redis_str(raw: Any) -> Optional[str]:
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    if isinstance(raw, bytearray):
        return bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        return raw
    return None


def _parse_iso_timestamp(ts_str: Any) -> Optional[float]:
    if not isinstance(ts_str, str) or not ts_str.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def set_incident_ttl(redis_client: Optional[redis.Redis], incident_id: str, ttl_seconds: int):
    if not redis_client or not isinstance(redis_client, redis.Redis) or ttl_seconds <= 0:
        return
    for suffix in _INCIDENT_TTL_SUFFIXES:
        redis_client.expire(f"incident:{incident_id}:{suffix}", ttl_seconds)
    if hasattr(redis_client, "scan_iter"):
        try:
            for act_key in redis_client.scan_iter(f"incident:{incident_id}:action:*"):
                redis_client.expire(act_key, ttl_seconds)
        except TypeError:
            pass


def archive_incident_to_disk(
    redis_client: Optional[redis.Redis],
    incident_id: str,
    storage_path: str = "/data/incidents",
    dumped_marker: Optional[str] = None,
) -> Optional[Path]:
    """Flush ledger entries and granular Redis keys for an incident to disk."""
    if not redis_client:
        return None

    entries = []
    if hasattr(redis_client, "lrange"):
        raw_entries = redis_client.lrange(f"incident:{incident_id}:ledger", 0, -1)
        if isinstance(raw_entries, (list, tuple)):
            entries = get_ledger_entries(redis_client, incident_id)

    snapshot: Dict[str, Any] = {}
    if hasattr(redis_client, "get"):
        for suffix in _SNAPSHOT_KEY_SUFFIXES:
            decoded = _decode_redis_str(redis_client.get(f"incident:{incident_id}:{suffix}"))
            if decoded is not None:
                try:
                    snapshot[suffix] = json.loads(decoded)
                except Exception:
                    snapshot[suffix] = decoded

    if not entries and not snapshot:
        return None

    inc_dir = dump_ledger_to_disk(
        storage_path=storage_path,
        incident_id=incident_id,
        entries=entries,
    )
    if snapshot:
        snapshot_file = inc_dir / "incident_snapshot.json"
        with open(snapshot_file, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)

    marker = dumped_marker or (snapshot.get("state", {}).get("updated_at") if isinstance(snapshot.get("state"), dict) else None) or datetime.now(timezone.utc).isoformat()
    if hasattr(redis_client, "set"):
        redis_client.set(f"incident:{incident_id}:dumped_at", str(marker))
    return inc_dir


def sweep_idle_incidents_once(
    redis_client: Optional[redis.Redis],
    ledger_config: Optional[LedgerConfig] = None,
    now_ts: Optional[float] = None,
) -> List[str]:
    """Archive inactive Redis incidents past dump_idle_seconds and enforce redis_ttl_seconds."""
    if not redis_client or not hasattr(redis_client, "scan_iter"):
        return []

    cfg = ledger_config or ConfigManager().system.ledger
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    archived: List[str] = []

    try:
        keys = list(redis_client.scan_iter("incident:inc-*:state"))
    except TypeError:
        return []

    for key in keys:
        raw = _decode_redis_str(redis_client.get(key))
        if not raw:
            continue
        try:
            state_data = json.loads(raw)
        except Exception:
            continue
        inc_id = state_data.get("incident_id")
        if not inc_id:
            continue

        state_val = state_data.get("state", "")
        ts_str = state_data.get("updated_at") or state_data.get("created_at")
        updated_ts = _parse_iso_timestamp(ts_str)
        idle_seconds = (now - updated_ts) if updated_ts is not None else 0.0

        is_idle = cfg.dump_idle_seconds > 0 and idle_seconds >= cfg.dump_idle_seconds
        is_closed_dump = state_val == "CLOSED" and cfg.dump_on_close

        if not (is_idle or is_closed_dump):
            continue

        dumped_marker = _decode_redis_str(redis_client.get(f"incident:{inc_id}:dumped_at"))
        if ts_str and dumped_marker != ts_str:
            out_dir = archive_incident_to_disk(
                redis_client,
                inc_id,
                storage_path=cfg.storage_path,
                dumped_marker=ts_str,
            )
            if out_dir is not None:
                archived.append(inc_id)

        if cfg.redis_ttl_seconds > 0 and hasattr(redis_client, "ttl"):
            try:
                cur_ttl = redis_client.ttl(f"incident:{inc_id}:state")
                if isinstance(cur_ttl, int) and cur_ttl == -1:
                    set_incident_ttl(redis_client, inc_id, cfg.redis_ttl_seconds)
            except Exception as exc:
                logger.warning("Failed setting TTL for archived incident %s: %s", inc_id, exc)

    return archived


async def run_idle_incident_archiver(
    redis_client: Optional[redis.Redis],
    ledger_config: Optional[LedgerConfig] = None,
    interval_seconds: int = 30,
) -> None:
    """Periodically flush idle Redis incidents to disk and apply Redis RAM TTLs."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if redis_client is not None:
                await asyncio.to_thread(sweep_idle_incidents_once, redis_client, ledger_config)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Idle incident archiver sweep error: %s", exc)


def update_incident_state(redis_client: Optional[redis.Redis], incident_id: str, new_state: str, from_state: Optional[str] = None) -> bool:
    if not redis_client:
        return False
    key = f"incident:{incident_id}:state"
    data = get_incident(redis_client, incident_id) or {}
    old_state = from_state or data.get("state", "NEW")
    data["state"] = new_state
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    visited = data.setdefault("visited_states", [])
    if not visited:
        visited.append(old_state)
    if new_state not in visited:
        visited.append(new_state)

    data["last_transition"] = {
        "from": old_state,
        "to": new_state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if hasattr(redis_client, "set"):
        redis_client.set(key, json.dumps(data))
    if new_state == "CLOSED":
        if hasattr(redis_client, "get"):
            raw_grp = _decode_redis_str(redis_client.get(f"incident:{incident_id}:alert_group"))
            if raw_grp:
                try:
                    grp_data = json.loads(raw_grp)
                    fps = grp_data.get("fingerprints", [])
                    for fp in fps:
                        if hasattr(redis_client, "delete"):
                            redis_client.delete(f"incident:fingerprints:{fp}")
                except Exception as e:
                    logger.warning(f"Failed to clear fingerprints for closed incident {incident_id}: {e}")
        try:
            ledger_cfg = ConfigManager().system.ledger
            if ledger_cfg.dump_on_close:
                archive_incident_to_disk(
                    redis_client,
                    incident_id,
                    storage_path=ledger_cfg.storage_path,
                    dumped_marker=data.get("updated_at"),
                )
            if ledger_cfg.redis_ttl_seconds > 0:
                set_incident_ttl(redis_client, incident_id, ledger_cfg.redis_ttl_seconds)
        except Exception as e:
            logger.warning(f"Failed to apply close archival/TTL for incident {incident_id}: {e}")
    return True
