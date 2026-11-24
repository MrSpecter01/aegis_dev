"""Unit tests for Aegis Telegram Bot FastAPI Service (services/telegram_bot/main.py)."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient

from services.telegram_bot.main import create_app
from services.telegram_bot.notifier import TelegramNotifier
from shared.config.schemas import SystemConfig, TelegramConfig


@pytest.fixture
def test_app():
    cfg = SystemConfig(telegram=TelegramConfig(bot_token="test-token", chat_id="12345", service_port=8088))
    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.dispatch = AsyncMock(return_value=True)

    app = create_app(config=cfg, bot_override=MagicMock(), notifier_override=mock_notifier)
    return app, mock_notifier


def test_telegram_service_health(test_app):
    app, _ = test_app
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["enabled"] is True
        assert data["bot_configured"] is True
        assert data["chat_configured"] is True


def test_telegram_service_notify_delivered(test_app):
    app, mock_notifier = test_app
    with TestClient(app) as client:
        payload = {
            "event": "incident_created",
            "incident_id": "inc-20261016-0001",
            "data": {
                "incident": {
                    "incident_id": "inc-20261016-0001",
                    "severity": "P1",
                    "affected_service": "cartservice",
                }
            },
        }
        resp = client.post("/notify", json=payload)
        assert resp.status_code == 200
        res_data = resp.json()
        assert res_data["status"] == "delivered"
        assert res_data["event"] == "incident_created"
        assert res_data["incident_id"] == "inc-20261016-0001"

        mock_notifier.dispatch.assert_called_once_with(
            event="incident_created",
            incident_id="inc-20261016-0001",
            data=payload["data"],
        )


def test_telegram_service_notify_skipped(test_app):
    app, mock_notifier = test_app
    mock_notifier.dispatch.return_value = False

    with TestClient(app) as client:
        payload = {
            "event": "unknown_event",
            "incident_id": "inc-20261016-0002",
            "data": {},
        }
        resp = client.post("/notify", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"


def test_telegram_service_notify_invalid_payload(test_app):
    app, _ = test_app
    with TestClient(app) as client:
        # Missing required fields 'event' and 'incident_id'
        resp = client.post("/notify", json={"invalid": "payload"})
        assert resp.status_code == 422
