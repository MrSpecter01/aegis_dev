"""Unit tests for API server notifications dispatcher and approval 409 / deny-wins logic."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from services.api_server.main import create_app
from services.api_server.notifications import dispatch_notification, dispatch_notification_background
from shared.config.schemas import SystemConfig, TelegramConfig
from tests.unit.test_correlator import CorrelatorFakeRedis
from tests.unit.test_sag_approval import _seed_incident_in_state


@pytest.mark.asyncio
async def test_dispatch_notification_success():
    cfg = SystemConfig(telegram=TelegramConfig(bot_url="http://mock-bot:8088"))
    mock_resp = MagicMock(status_code=200)

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        res = await dispatch_notification(
            event="incident_created",
            incident_id="inc-101",
            data={"service": "cartservice"},
            config=cfg,
        )
        assert res is True
        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "http://mock-bot:8088/notify"
        payload = mock_post.call_args[1]["json"]
        assert payload["event"] == "incident_created"
        assert payload["incident_id"] == "inc-101"
        assert payload["data"] == {"service": "cartservice"}


@pytest.mark.asyncio
async def test_dispatch_notification_failure_graceful():
    cfg = SystemConfig(telegram=TelegramConfig(bot_url="http://mock-bot:8088"))
    mock_resp = MagicMock(status_code=500, text="Internal Server Error")

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        res = await dispatch_notification(
            event="triage_summary",
            incident_id="inc-102",
            config=cfg,
        )
        assert res is False


@pytest.mark.asyncio
async def test_dispatch_notification_no_url_skips():
    cfg = SystemConfig(telegram=TelegramConfig(bot_url=""))
    res = await dispatch_notification(
        event="incident_created",
        incident_id="inc-103",
        config=cfg,
    )
    assert res is False


@pytest.mark.asyncio
async def test_dispatch_notification_background():
    with patch("services.api_server.notifications.dispatch_notification", new_callable=AsyncMock) as mock_disp:
        dispatch_notification_background(
            event="escalation",
            incident_id="inc-104",
        )
        # Give event loop a micro-yield to run created task
        import asyncio
        await asyncio.sleep(0.01)
        mock_disp.assert_called_once()


def test_approve_endpoint_double_tap_409(mocker):
    """Pressing approve twice returns 409 Conflict per M10 DoD."""
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-double-tap-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Already approved action
    fake_redis.set(
        f"incident:{incident_id}:action:act-dt-01",
        json.dumps({"action_id": "act-dt-01", "status": "approved"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        # Second approve click -> 409
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-dt-01",
                "decision": "approved",
                "actor": "telegram_user",
                "channel": "telegram",
            },
        )
        assert resp.status_code == 409
        assert "already been decided" in resp.json()["detail"]


def test_approve_endpoint_deny_overrides_approved(mocker):
    """Hard Rule 9: Deny wins. A denial must be able to override an earlier approved decision."""
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-deny-wins-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Currently approved action
    fake_redis.set(
        f"incident:{incident_id}:action:act-dw-01",
        json.dumps({"action_id": "act-dw-01", "status": "approved"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        # Deny from dashboard -> MUST succeed (deny wins over approved)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-dw-01",
                "decision": "denied",
                "actor": "lead_sre",
                "channel": "dashboard",
                "reason": "Aborting remediation manually",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"

    # Action record in store must now be 'denied'
    raw = fake_redis.get(f"incident:{incident_id}:action:act-dw-01")
    record = json.loads(raw)
    assert record["status"] == "denied"
    assert record["decided_by"] == "lead_sre"


def test_approve_endpoint_denied_can_be_approved_later(mocker):
    """Action once denied can be approved later, and late denial returns to denied."""
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-deny-reapprove-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Currently denied action
    fake_redis.set(
        f"incident:{incident_id}:action:act-df-01",
        json.dumps({"action_id": "act-df-01", "status": "denied"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        # 1. Action once denied can be approved later -> 200
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-df-01",
                "decision": "approved",
                "actor": "telegram_user",
                "channel": "telegram",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Verify action record is now approved
        raw = fake_redis.get(f"incident:{incident_id}:action:act-df-01")
        assert json.loads(raw)["status"] == "approved"

        # 2. Deny after approval -> MUST succeed, record becomes denied
        resp2 = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-df-01",
                "decision": "denied",
                "actor": "commander",
                "channel": "dashboard",
                "reason": "Late denial after review",
            },
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "denied"

        # Current state of action must be in denied
        raw2 = fake_redis.get(f"incident:{incident_id}:action:act-df-01")
        assert json.loads(raw2)["status"] == "denied"


def test_dashboard_denial_dispatches_notification(mocker):
    """Denial from dashboard triggers cross-channel notification to telegram bot."""
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-notif-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    fake_redis.set(
        f"incident:{incident_id}:action:act-notif-01",
        json.dumps({"action_id": "act-notif-01", "status": "pending"}),
    )

    with patch("services.api_server.notifications.dispatch_notification_background") as mock_disp:
        headers = {"X-API-Key": app.state.config.system.system.api_key}
        with TestClient(app) as client:
            client.headers.update(headers)
            resp = client.post(
                f"/v1/incidents/{incident_id}/approve",
                json={
                    "action_id": "act-notif-01",
                    "decision": "denied",
                    "actor": "lead_sre",
                    "channel": "dashboard",
                },
            )
            assert resp.status_code == 200
            assert mock_disp.call_count == 2
            first_call = mock_disp.call_args_list[0][1]
            assert first_call["event"] == "denial_notification"
            assert first_call["incident_id"] == "inc-notif-01"
            assert first_call["data"]["channel"] == "dashboard"

            second_call = mock_disp.call_args_list[1][1]
            assert second_call["event"] == "escalation"
            assert second_call["incident_id"] == "inc-notif-01"


@pytest.mark.asyncio
async def test_dispatch_notification_disabled_skips():
    """When telegram.enabled is false, all notifications are skipped."""
    cfg = SystemConfig(telegram=TelegramConfig(enabled=False, bot_url="http://mock-bot:8088"))
    with patch("httpx.AsyncClient.post") as mock_post:
        res = await dispatch_notification(
            event="incident_created",
            incident_id="inc-skip-01",
            config=cfg,
        )
        assert res is False
        mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_notification_granular_event_toggle_skips():
    """Granular toggles like notify_on_triage allow selective filtering of intermediate chatter."""
    cfg = SystemConfig(
        telegram=TelegramConfig(
            enabled=True,
            bot_url="http://mock-bot:8088",
            notify_on_triage=False,
            notify_on_approval=True,
        )
    )
    mock_resp = MagicMock(status_code=200)
    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        # Triage summary must be skipped (notify_on_triage = False)
        res_triage = await dispatch_notification(
            event="triage_summary",
            incident_id="inc-triage-skip",
            config=cfg,
        )
        assert res_triage is False
        mock_post.assert_not_called()

        # Approval request must proceed (notify_on_approval = True)
        res_approval = await dispatch_notification(
            event="approval_request",
            incident_id="inc-approval-proceed",
            config=cfg,
        )
        assert res_approval is True
        mock_post.assert_called_once()

