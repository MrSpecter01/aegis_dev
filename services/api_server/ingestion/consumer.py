import asyncio
import json
import logging
from typing import Optional, Any

from shared.config.schemas import SystemConfig
from services.api_server.ingestion.correlator import AlertCorrelator
from services.api_server.topology.store import TopologyStore

logger = logging.getLogger("aegis.ingestion.consumer")


def _parse_payload(raw_payload: Any) -> Optional[dict]:
    """Parse payload from string, bytes, or dict into a dictionary."""
    if not raw_payload:
        return None
    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8")
    if isinstance(raw_payload, str):
        try:
            return json.loads(raw_payload)
        except Exception:
            logger.error(f"Failed to parse payload: {raw_payload[:100]}")
            return None
    if isinstance(raw_payload, dict):
        return raw_payload
    return None


def _delivery_count(redis_client: Any, stream_key: str, group_name: str, msg_id: Any) -> int:
    """Query Redis for the number of delivery attempts for a pending message."""
    if hasattr(redis_client, "xpending_range"):
        try:
            pending = redis_client.xpending_range(stream_key, group_name, min=msg_id, max=msg_id, count=1)
            if pending:
                first = pending[0]
                if isinstance(first, dict):
                    return int(first.get("times_delivered", 1))
                if isinstance(first, (list, tuple)) and len(first) >= 4:
                    return int(first[3])
        except Exception as e:
            logger.warning(f"Failed to query xpending_range for {msg_id}: {e}")
    return 1


def _extract_ingest_meta(data: dict) -> Optional[dict]:
    """Decode optional webhook caller attribution metadata from raw stream fields."""
    def _val(key: str) -> Optional[str]:
        raw = data.get(key)
        if raw is None:
            raw = data.get(key.encode("utf-8"))
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        cleaned = str(raw).strip() if raw is not None else ""
        return cleaned or None

    ingested_by = _val("ingested_by")
    if not ingested_by:
        return None
    return {
        "ingested_by": ingested_by,
        "ingested_by_id": _val("ingested_by_id"),
        "ingested_entity_type": _val("ingested_entity_type"),
        "ingested_key_id": _val("ingested_key_id"),
    }


async def run_alert_consumer(
    redis_client: Any,
    config: SystemConfig,
    topology_store: Optional[TopologyStore] = None,
    run_once: bool = False,
) -> None:
    """
    Background worker consuming raw alerts from Redis Stream 'stream:alert:raw'.
    Deduplicates, groups, and enriches via AlertCorrelator.
    """
def _init_consumer_group(redis_client: Any, stream_key: str, group_name: str) -> None:
    """Ensure consumer group exists on the stream, creating the stream if needed."""
    if hasattr(redis_client, "xgroup_create"):
        try:
            redis_client.xgroup_create(stream_key, group_name, id="0", mkstream=True)
            logger.info(f"Created consumer group '{group_name}' on stream '{stream_key}'")
        except Exception as e:
            if "BUSYGROUP" in str(e):
                logger.debug(f"Consumer group '{group_name}' already exists: {e}")
            else:
                logger.warning(f"Error creating consumer group '{group_name}': {e}")


def _extract_ingest_meta(data: dict) -> Optional[dict]:
    """Decode optional webhook caller attribution metadata from raw stream fields."""
    def _val(key: str) -> Optional[str]:
        raw = data.get(key)
        if raw is None:
            raw = data.get(key.encode("utf-8"))
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        cleaned = str(raw).strip() if raw is not None else ""
        return cleaned or None

    ingested_by = _val("ingested_by")
    if not ingested_by:
        return None
    return {
        "ingested_by": ingested_by,
        "ingested_by_id": _val("ingested_by_id"),
        "ingested_entity_type": _val("ingested_entity_type"),
        "ingested_key_id": _val("ingested_key_id"),
    }


async def run_alert_consumer(
    redis_client: Any,
    config: SystemConfig,
    topology_store: Optional[TopologyStore] = None,
    run_once: bool = False,
) -> None:
    """
    Background worker consuming raw alerts from Redis Stream 'stream:alert:raw'.
    Deduplicates, groups, and enriches via AlertCorrelator.
    """
    group_name = config.ingestion.consumer_group
    consumer_name = config.ingestion.consumer_name
    stream_key = "stream:alert:raw"

    # Idempotent consumer group creation
    _init_consumer_group(redis_client, stream_key, group_name)

    ing_allowed = getattr(config.ingestion, "allowed_namespaces", None)
    topo_allowed = getattr(getattr(config, "topology", None), "monitored_namespaces", None)
    allowed_ns = (
        ing_allowed
        if isinstance(ing_allowed, list) and ing_allowed
        else (topo_allowed if isinstance(topo_allowed, list) and topo_allowed else None)
    )
    correlator = AlertCorrelator(
        redis_client,
        config.ingestion,
        topology_store,
        allowed_namespaces=allowed_ns,
    )

    while True:
        try:
            if not hasattr(redis_client, "xreadgroup"):
                if run_once:
                    break
                await asyncio.sleep(0.5)
                continue

            streams = await asyncio.to_thread(
                redis_client.xreadgroup,
                group_name,
                consumer_name,
                {stream_key: ">"},
                count=10,
                block=1000,
            )

            if not streams:
                if run_once:
                    break
                await asyncio.sleep(0.05)
                continue

            for stream_name, messages in streams:
                for msg_id, data in messages:
                    raw_payload = data.get("payload") or data.get(b"payload")
                    ingest_meta = _extract_ingest_meta(data)
                    try:
                        parsed = _parse_payload(raw_payload)
                        if parsed is None:
                            raise ValueError("unparseable payload")
                        if ingest_meta is not None:
                            await correlator.process(parsed, ingest_meta=ingest_meta)
                        else:
                            await correlator.process(parsed)
                        if hasattr(redis_client, "xack"):
                            await asyncio.to_thread(redis_client.xack, stream_key, group_name, msg_id)
                    except Exception as e:
                        attempts = _delivery_count(redis_client, stream_key, group_name, msg_id)
                        logger.error("alert %s failed (attempt %d): %s", msg_id, attempts, e, exc_info=True)
                        max_attempts = getattr(config.ingestion, "max_delivery_attempts", 3)
                        if attempts >= max_attempts:
                            if hasattr(redis_client, "xadd"):
                                await asyncio.to_thread(
                                    redis_client.xadd,
                                    "stream:alert:dead",
                                    {"payload": str(raw_payload), "error": str(e)},
                                )
                            if hasattr(redis_client, "xack"):
                                await asyncio.to_thread(redis_client.xack, stream_key, group_name, msg_id)

            if run_once:
                break

        except asyncio.CancelledError:
            logger.info("Alert consumer task received cancel signal. Shutting down gracefully.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in alert consumer loop: {e}")
            if "NOGROUP" in str(e):
                try:
                    _init_consumer_group(redis_client, stream_key, group_name)
                except Exception as ge:
                    logger.warning(f"Failed to recreate consumer group: {ge}")
            if run_once:
                break
            await asyncio.sleep(1.0)
