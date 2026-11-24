"""Notification dispatcher for the Aegis Control Plane API.

Pushes incident lifecycle events asynchronously to the Aegis Telegram Bot service (POST /notify)
in a non-blocking fire-and-forget pattern.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional
import httpx

from shared.config.loader import load_system_config
from shared.config.schemas import SystemConfig
from shared.models.notifications import NotificationPayload

logger = logging.getLogger("aegis.api_server.notifications")


async def dispatch_notification(
    event: str,
    incident_id: str,
    data: Optional[Dict[str, Any]] = None,
    config: Optional[SystemConfig] = None,
    bot_url_override: Optional[str] = None,
) -> bool:
    """Send notification payload to the Telegram Bot service."""
    try:
        cfg = config or load_system_config()
        tg_cfg = getattr(cfg, "telegram", None)
        if tg_cfg is not None:
            if not getattr(tg_cfg, "enabled", True):
                logger.debug("Telegram notifications are disabled via config (telegram.enabled=false).")
                return False

            if event == "incident_created" and not getattr(tg_cfg, "notify_on_creation", True):
                logger.debug("Creation notification skipped (telegram.notify_on_creation=false).")
                return False
            elif event == "triage_summary" and not getattr(tg_cfg, "notify_on_triage", True):
                logger.debug("Triage notification skipped (telegram.notify_on_triage=false).")
                return False
            elif event == "approval_request" and not getattr(tg_cfg, "notify_on_approval", True):
                logger.debug("Approval notification skipped (telegram.notify_on_approval=false).")
                return False
            elif event == "escalation" and not getattr(tg_cfg, "notify_on_escalation", True):
                logger.debug("Escalation notification skipped (telegram.notify_on_escalation=false).")
                return False
            elif event == "report_delivered" and not getattr(tg_cfg, "notify_on_closure", True):
                logger.debug("Closure notification skipped (telegram.notify_on_closure=false).")
                return False

        bot_url = bot_url_override or os.getenv("TELEGRAM_BOT_URL") or getattr(tg_cfg, "bot_url", "")
        if not bot_url:
            logger.debug("Telegram bot_url is unconfigured; skipping notification dispatch.")
            return False

        endpoint = f"{bot_url.rstrip('/')}/notify"
        payload = NotificationPayload(
            event=event,
            incident_id=incident_id,
            data=data or {},
        )

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(endpoint, json=payload.model_dump(mode="json"))
            if resp.status_code in (200, 202):
                logger.info(
                    "Dispatched notification '%s' for incident '%s' to %s",
                    event,
                    incident_id,
                    endpoint,
                )
                return True
            else:
                logger.warning(
                    "Telegram bot /notify returned status %d for event '%s': %s",
                    resp.status_code,
                    event,
                    resp.text,
                )
                return False
    except Exception as exc:
        logger.warning(
            "Failed to dispatch notification '%s' for incident '%s': %s",
            event,
            incident_id,
            exc,
        )
        return False


def dispatch_notification_background(
    event: str,
    incident_id: str,
    data: Optional[Dict[str, Any]] = None,
    config: Optional[SystemConfig] = None,
    bot_url_override: Optional[str] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> None:
    """Fire-and-forget notification dispatch scheduled as an asyncio background task."""
    coro = dispatch_notification(
        event=event,
        incident_id=incident_id,
        data=data,
        config=config,
        bot_url_override=bot_url_override,
    )
    try:
        active_loop = asyncio.get_running_loop()
        active_loop.create_task(coro)
    except RuntimeError:
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            coro.close()
            logger.warning(
                "No active event loop available; notification '%s' for %s dropped",
                event,
                incident_id,
            )
