"""Delivery adapter for Phase 3 Scribe incident reports.

Persists reports and raw ledger dumps to disk, dispatches reports as document attachments
to Telegram, and invokes the Aegis Control Plane API state machine transition callback.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

from shared.config.loader import get_config
from shared.ledger.writer import dump_ledger_to_disk
from shared.models.ledger import IncidentReport, LedgerEntry

logger = logging.getLogger("phase3_worker.delivery")


async def send_telegram_document(
    bot_token: str,
    chat_id: str,
    document_path: Path,
    caption: str,
    telegram_bot_client: Optional[Any] = None,
) -> bool:
    """Send incident report file as a document attachment to Telegram."""
    if not bot_token or not chat_id:
        logger.warning("Telegram bot_token or chat_id unconfigured; skipping Telegram delivery.")
        return False

    # Allow injected client/bot for unit testing
    if telegram_bot_client is not None:
        try:
            if hasattr(telegram_bot_client, "send_document"):
                res = telegram_bot_client.send_document(
                    chat_id=chat_id,
                    document=document_path,
                    caption=caption,
                )
                if hasattr(res, "__await__"):
                    await res
                return True
        except Exception as e:
            logger.error(f"Error sending document via injected Telegram client: {e}")
            return False

    # Live HTTP dispatch via Telegram Bot API sendDocument
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            with open(document_path, "rb") as f:
                files = {"document": (document_path.name, f, "text/markdown")}
                data = {"chat_id": chat_id, "caption": caption}
                resp = await client.post(url, data=data, files=files)
                if resp.status_code == 200:
                    logger.info("Successfully delivered report document to Telegram chat %s", chat_id)
                    return True
                else:
                    logger.warning("Telegram API sendDocument returned status %d: %s", resp.status_code, resp.text)
                    return False
    except Exception as exc:
        logger.error("Exception dispatching document to Telegram: %s", exc)
        return False


async def notify_api_state_transition(
    api_url: str,
    api_key: str,
    incident_id: str,
    event: str = "report_delivered",
    actor: str = "scribe",
    metadata: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Invoke the Aegis Control Plane API transition endpoint."""
    url = f"{api_url.rstrip('/')}/v1/incidents/{incident_id}/transition"
    payload = {
        "event": event,
        "actor": actor,
        "metadata": metadata or {},
    }
    headers = {
        "Content-Type": "application/json",
        "X-Aegis-Service": "phase3-worker",
    }
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        if client:
            resp = await client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=10.0) as default_client:
                resp = await default_client.post(url, json=payload, headers=headers)

        if resp.status_code == 200:
            logger.info("Successfully transitioned incident %s via event '%s': %s", incident_id, event, resp.json())
            return True
        else:
            logger.warning("Failed transition for %s (status %d): %s", incident_id, resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error("Error calling transition endpoint for incident %s: %s", incident_id, exc)
        return False


async def deliver_report(
    incident_id: str,
    report: IncidentReport,
    ledger_entries: List[LedgerEntry],
    topology_snapshot: Optional[Dict[str, Any]] = None,
    storage_path: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    telegram_bot_client: Optional[Any] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Execute complete report delivery: disk persistence and API callback.
    Telegram notification and document dispatch are handled by the Telegram Bot service on report_delivered event."""
    config = get_config()
    target_storage = storage_path or config.system.ledger.storage_path
    target_api_url = (
        api_url
        or os.getenv("AEGIS_INTERNAL_API_URL")
        or os.getenv("AEGIS_API_URL")
        or config.system.system.api_url
    ).rstrip("/")
    target_api_key = api_key if api_key is not None else (os.getenv("AEGIS_API_KEY") or config.system.system.api_key)

    # 1. Disk persistence (writes report.md, ledger_full.json, topology_snapshot.json)
    out_dir = dump_ledger_to_disk(
        storage_path=target_storage,
        incident_id=incident_id,
        entries=ledger_entries,
        report=report,
        topology_snapshot=topology_snapshot,
    )
    report_file = out_dir / "report.md"

    # 2. Control Plane API callback to advance state to CLOSED
    # The state machine's transition to CLOSED triggers notification dispatch to the bot, which sends report.md
    delivered_channels = ["disk"]

    transition_metadata = {
        "report_path": str(report_file),
        "delivered_to": delivered_channels,
    }

    api_ok = await notify_api_state_transition(
        api_url=target_api_url,
        api_key=target_api_key,
        incident_id=incident_id,
        event="report_delivered",
        actor="scribe",
        metadata=transition_metadata,
        client=http_client,
    )

    return {
        "disk_dir": str(out_dir),
        "report_file": str(report_file),
        "telegram_delivered": False,
        "transition_successful": api_ok,
    }
