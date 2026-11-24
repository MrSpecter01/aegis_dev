"""Aegis Telegram Bot Service.

Runs an ASGI FastAPI HTTP service on port 8088 to expose POST /notify for internal push notifications,
and manages the python-telegram-bot Application polling loop within the same asyncio event loop.
"""

import contextlib
import logging
import os
from typing import Any, Optional
from fastapi import FastAPI, HTTPException
import uvicorn
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from shared.config.loader import load_system_config
from shared.config.schemas import SystemConfig
from shared.models.notifications import NotificationPayload
from services.telegram_bot.handlers import (
    approval_callback,
    approve_command,
    approve_drift_command,
    close_command,
    deny_command,
    drop_command,
    escalate_command,
    help_command,
    incident_detail_command,
    incidents_command,
    reject_drift_command,
    start_command,
    status_command,
    steer_command,
)
from services.telegram_bot.notifier import TelegramNotifier

logger = logging.getLogger("aegis.telegram_bot.service")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for unhandled Telegram bot exceptions."""
    logger.error("Exception handling Telegram update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An internal error occurred while processing this command. Please check system logs."
            )
        except Exception as exc:
            logger.warning("Failed to notify user about error: %s", exc)


def build_telegram_application(
    bot_token: str,
    api_url: str,
    api_key: str,
    chat_id: str = "",
) -> Optional[Application]:
    """Construct python-telegram-bot Application and register command/callback handlers."""
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN is empty; bot polling will be disabled.")
        return None

    app = ApplicationBuilder().token(bot_token).build()

    # Store API connection parameters in bot_data for handlers
    app.bot_data["aegis_api_url"] = api_url
    app.bot_data["aegis_api_key"] = api_key
    app.bot_data["chat_id"] = str(chat_id)

    # Register slash commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("incidents", incidents_command))
    app.add_handler(CommandHandler("list", incidents_command))
    app.add_handler(CommandHandler("incident", incident_detail_command))
    app.add_handler(CommandHandler("steer", steer_command))
    app.add_handler(CommandHandler("escalate", escalate_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("deny", deny_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("drop", drop_command))
    app.add_handler(CommandHandler("approve_drift", approve_drift_command))
    app.add_handler(CommandHandler("reject_drift", reject_drift_command))

    # Register inline button callback query handler
    app.add_handler(CallbackQueryHandler(approval_callback))

    # Register global error handler
    app.add_error_handler(global_error_handler)

    return app


def create_app(
    config: Optional[SystemConfig] = None,
    bot_override: Optional[Any] = None,
    notifier_override: Optional[TelegramNotifier] = None,
) -> FastAPI:
    """Create the FastAPI service with embedded python-telegram-bot polling lifespan."""
    cfg = config or load_system_config()

    bot_token = cfg.telegram.bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or ""
    chat_id = cfg.telegram.chat_id or os.getenv("TELEGRAM_CHAT_ID") or ""
    api_url = os.getenv("AEGIS_INTERNAL_API_URL") or os.getenv("AEGIS_API_URL") or cfg.system.api_url
    api_key = cfg.system.api_key or os.getenv("AEGIS_API_KEY") or ""

    telegram_app: Optional[Application] = None
    if not bot_override and bot_token:
        try:
            telegram_app = build_telegram_application(bot_token, api_url, api_key, chat_id=chat_id)
        except Exception as exc:
            logger.error("Failed to build Telegram Application: %s", exc)

    bot_instance = bot_override or (telegram_app.bot if telegram_app else None)
    notifier = notifier_override or TelegramNotifier(bot=bot_instance, default_chat_id=chat_id)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: start polling if bot application initialized and enabled
        if telegram_app is not None and getattr(cfg.telegram, "enabled", True):
            try:
                await telegram_app.initialize()
                await telegram_app.start()
                if telegram_app.updater is not None:
                    await telegram_app.updater.start_polling()
                logger.info("Telegram bot polling started successfully.")
            except Exception as exc:
                logger.error("Error starting Telegram bot polling: %s", exc)
        elif telegram_app is not None:
            logger.info("Telegram bot polling skipped because telegram.enabled is false.")

        yield

        # Shutdown: stop polling gracefully
        if telegram_app is not None:
            try:
                if telegram_app.updater is not None and telegram_app.updater.running:
                    await telegram_app.updater.stop()
                await telegram_app.stop()
                await telegram_app.shutdown()
                logger.info("Telegram bot polling shut down.")
            except Exception as exc:
                logger.error("Error during Telegram bot shutdown: %s", exc)

    app = FastAPI(title="Aegis Telegram Bot Service", lifespan=lifespan)
    app.state.notifier = notifier
    app.state.telegram_app = telegram_app
    app.state.config = cfg

    @app.post("/notify")
    async def notify_endpoint(payload: NotificationPayload):
        """Internal endpoint for Aegis API server to push incident events."""
        current_notifier: TelegramNotifier = getattr(app.state, "notifier", notifier)
        success = await current_notifier.dispatch(
            event=payload.event,
            incident_id=payload.incident_id,
            data=payload.data,
        )
        return {"status": "delivered" if success else "skipped", "event": payload.event, "incident_id": payload.incident_id}

    @app.get("/health")
    async def health_endpoint():
        """Healthcheck endpoint for Docker Compose."""
        return {
            "status": "ok",
            "enabled": bool(getattr(cfg.telegram, "enabled", True)),
            "bot_configured": bool(bot_token),
            "chat_configured": bool(chat_id),
            "bot_polling": bool(telegram_app and telegram_app.updater and telegram_app.updater.running),
        }

    return app


if __name__ == "__main__":
    sys_config = load_system_config()
    port = sys_config.telegram.service_port
    app = create_app(sys_config)
    uvicorn.run(app, host="0.0.0.0", port=port)
