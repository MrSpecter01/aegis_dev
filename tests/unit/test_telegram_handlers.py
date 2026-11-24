"""Unit tests for Telegram command and callback handlers (services/telegram_bot/handlers.py)."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.telegram_bot.handlers import (
    approval_callback,
    approve_command,
    close_command,
    deny_command,
    drop_command,
    escalate_command,
    help_command,
    incident_detail_command,
    incidents_command,
    start_command,
    steer_command,
)


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.bot_data = {
        "aegis_api_url": "http://mock-api:8080",
        "aegis_api_key": "test-key-123",
        "chat_id": "123456",
    }
    ctx.args = []
    return ctx


@pytest.fixture
def mock_update():
    up = MagicMock()
    up.effective_message = MagicMock()
    up.effective_message.reply_text = AsyncMock()
    up.effective_chat = MagicMock(id=123456)
    up.effective_user = MagicMock(id=1001, username="sre_alice", first_name="Alice")
    return up


@pytest.mark.asyncio
async def test_start_command_returns_system_status(mock_update, mock_context):
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {"status": "ok", "redis": "ok"}

    incidents_resp = MagicMock(status_code=200)
    incidents_resp.json.return_value = [
        {"incident_id": "inc-1", "state": "TRIAGE"},
        {"incident_id": "inc-2", "state": "AWAITING_APPROVAL"},
    ]

    with patch("httpx.AsyncClient.get", side_effect=[health_resp, incidents_resp]):
        await start_command(mock_update, mock_context)

    mock_update.effective_message.reply_text.assert_called_once()
    reply_text = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Aegis Incident Response Bot" in reply_text
    assert "Online (redis: ok)" in reply_text
    assert "Active Incidents:* `2`" in reply_text
    assert "TRIAGE: 1" in reply_text
    assert "AWAITING_APPROVAL: 1" in reply_text


@pytest.mark.asyncio
async def test_help_command(mock_update, mock_context):
    await help_command(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_called_once()
    text = mock_update.effective_message.reply_text.call_args[0][0]
    assert "/steer" in text
    assert "[✅ Approve]" in text
    assert "[❌ Deny]" in text


@pytest.mark.asyncio
async def test_incidents_command_lists_open_incidents(mock_update, mock_context):
    resp = MagicMock(status_code=200)
    resp.json.return_value = [
        {"incident_id": "inc-p1", "severity": "P1", "state": "TRIAGE", "affected_service": "frontend"},
        {"incident_id": "inc-p2", "severity": "P2", "state": "REMEDIATION", "affected_service": "cartservice"},
    ]

    with patch("httpx.AsyncClient.get", return_value=resp):
        await incidents_command(mock_update, mock_context)

    mock_update.effective_message.reply_text.assert_called_once()
    text = mock_update.effective_message.reply_text.call_args[0][0]
    assert "inc-p1" in text
    assert "frontend" in text
    assert "inc-p2" in text
    assert "cartservice" in text


@pytest.mark.asyncio
async def test_incident_detail_command(mock_update, mock_context):
    mock_context.args = ["inc-detail-01"]
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "incident_id": "inc-detail-01",
        "state": "AWAITING_APPROVAL",
        "severity": "P2",
        "affected_service": "redis-cart",
        "affected_namespace": "boutique",
        "retry_count": 1,
    }

    with patch("httpx.AsyncClient.get", return_value=resp):
        await incident_detail_command(mock_update, mock_context)

    mock_update.effective_message.reply_text.assert_called_once()
    text = mock_update.effective_message.reply_text.call_args[0][0]
    assert "inc-detail-01" in text
    assert "redis-cart" in text
    assert "AWAITING_APPROVAL" in text


@pytest.mark.asyncio
async def test_steer_command(mock_update, mock_context):
    mock_context.args = ["inc-steer-01", "Scale", "deployment", "to", "5"]
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"status": "steered"}

    with patch("httpx.AsyncClient.post", return_value=resp) as mock_post:
        await steer_command(mock_update, mock_context)

    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args[0][0] == "http://mock-api:8080/v1/incidents/inc-steer-01/steer"
    assert call_args[1]["headers"]["X-Aegis-Service"] == "aegis-telegram-bot"
    assert call_args[1]["json"]["instruction"] == "Scale deployment to 5"
    assert call_args[1]["json"]["actor"] == "sre_alice"
    assert call_args[1]["json"]["telegram_user_id"] == "1001"
    assert call_args[1]["json"]["telegram_username"] == "sre_alice"
    assert call_args[1]["json"]["channel"] == "telegram"

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Steering Guidance Applied" in reply
    assert "Scale deployment to 5" in reply


@pytest.mark.asyncio
async def test_escalate_command(mock_update, mock_context):
    mock_context.args = ["inc-esc-01", "Database", "pool", "locked"]
    resp = MagicMock(status_code=200)

    with patch("httpx.AsyncClient.post", return_value=resp) as mock_post:
        await escalate_command(mock_update, mock_context)

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "http://mock-api:8080/v1/incidents/inc-esc-01/escalate"
    assert mock_post.call_args[1]["json"]["reason"] == "Database pool locked"
    mock_update.effective_message.reply_text.assert_called_once()


@pytest.mark.asyncio
async def test_close_command(mock_update, mock_context):
    mock_context.args = ["inc-close-01", "Resolved", "manually"]
    resp = MagicMock(status_code=200)

    with patch("httpx.AsyncClient.post", return_value=resp) as mock_post:
        await close_command(mock_update, mock_context)

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "http://mock-api:8080/v1/incidents/inc-close-01/close"
    mock_update.effective_message.reply_text.assert_called_once()


@pytest.mark.asyncio
async def test_approval_callback_approve_success(mock_context):
    query = MagicMock()
    query.data = "approve:inc-01:act-01"
    query.from_user = MagicMock(username="oncall_bob", first_name="Bob")
    query.message = MagicMock(text="Original Action Proposal Text")
    query.message.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=123456)
    query.from_user.id = 1002

    resp = MagicMock(status_code=200)
    with patch("httpx.AsyncClient.post", return_value=resp) as mock_post:
        await approval_callback(update, mock_context)

    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    payload = mock_post.call_args[1]["json"]

    assert url == "http://mock-api:8080/v1/incidents/inc-01/approve"
    assert payload["action_id"] == "act-01"
    assert payload["decision"] == "approved"
    assert payload["actor"] == "oncall_bob"
    assert payload["telegram_user_id"] == "1002"
    assert payload["telegram_username"] == "oncall_bob"
    assert payload["channel"] == "telegram"

    query.answer.assert_called_once_with("Action Approved!")
    query.edit_message_text.assert_called_once()
    edited_text = query.edit_message_text.call_args[1]["text"]
    assert "Approved by oncall_bob" in edited_text
    assert query.edit_message_text.call_args[1]["reply_markup"] is None


@pytest.mark.asyncio
async def test_approval_callback_deny_success(mock_context):
    query = MagicMock()
    query.data = "deny:inc-01:act-01"
    query.from_user = MagicMock(id=1002, username="oncall_bob", first_name="Bob")
    query.message = MagicMock(text="Original Action Proposal Text")
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=123456)

    resp = MagicMock(status_code=200)
    with patch("httpx.AsyncClient.post", return_value=resp) as mock_post:
        await approval_callback(update, mock_context)

    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert payload["decision"] == "denied"
    assert payload["actor"] == "oncall_bob"
    assert payload["telegram_user_id"] == "1002"

    query.answer.assert_called_once_with("Action Denied!")
    query.edit_message_text.assert_called_once()
    edited_text = query.edit_message_text.call_args[1]["text"]
    assert "Denied by oncall_bob" in edited_text


@pytest.mark.asyncio
async def test_approval_callback_double_tap_409(mock_context):
    query = MagicMock()
    query.data = "approve:inc-01:act-01"
    query.from_user = MagicMock(id=1002, username="oncall_bob", first_name="Bob")
    query.message = MagicMock(text="Original Action Proposal Text")
    query.message.edit_message_reply_markup = AsyncMock()
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=123456)

    resp = MagicMock(status_code=409)
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"detail": "Action 'act-01' has already been decided: 'approved'"}

    with patch("httpx.AsyncClient.post", return_value=resp):
        await approval_callback(update, mock_context)

    # Must reply with "Already decided" per DoD
    query.answer.assert_called_once_with("Already decided", show_alert=True)
    query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_approval_callback_steer_prompts_instruction(mock_context):
    query = MagicMock()
    query.data = "steer:inc-01:act-01"
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    query.answer = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=123456)

    await approval_callback(update, mock_context)

    query.answer.assert_called_once()
    query.message.reply_text.assert_called_once()
    text = query.message.reply_text.call_args[0][0]
    assert "/steer inc-01" in text


@pytest.mark.asyncio
async def test_command_from_unknown_chat_ignored_and_logged(mock_update, mock_context):
    # Chat ID does not match authorized chat_id (123456)
    mock_update.effective_chat.id = 999999
    with patch("httpx.AsyncClient.get") as mock_get:
        await start_command(mock_update, mock_context)
        mock_get.assert_not_called()
    mock_update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_callback_from_unknown_chat_does_not_call_api(mock_context):
    query = MagicMock()
    query.data = "approve:inc-01:act-01"
    query.answer = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=999999)  # unknown

    with patch("httpx.AsyncClient.post") as mock_post:
        await approval_callback(update, mock_context)
        mock_post.assert_not_called()

    query.answer.assert_called_once_with("Not authorised", show_alert=True)


@pytest.mark.asyncio
async def test_empty_allow_list_denies_all(mock_update, mock_context):
    # Empty chat_id in config/bot_data denies all
    mock_context.bot_data["chat_id"] = ""
    with patch("httpx.AsyncClient.get") as mock_get:
        await start_command(mock_update, mock_context)
        mock_get.assert_not_called()
    mock_update.effective_message.reply_text.assert_not_called()


def test_callback_data_length_within_telegram_limit():
    """Verify callback data with long incident/action IDs stays under Telegram's 64-byte limit."""
    incident_id = "inc-20261020-9999"
    action_id = "act-inc-20261020-9999-deadbeef"

    approve_cb = f"approve:{incident_id}:{action_id}"
    deny_cb = f"deny:{incident_id}:{action_id}"
    steer_cb = f"steer:{incident_id}:{action_id}"

    assert len(approve_cb.encode("utf-8")) <= 64
    assert len(deny_cb.encode("utf-8")) <= 64
    assert len(steer_cb.encode("utf-8")) <= 64

    # Also verify parsing with split(":", 2) works
    parts = approve_cb.split(":", 2)
    assert len(parts) == 3
    assert parts[0] == "approve"
    assert parts[1] == incident_id
    assert parts[2] == action_id


@pytest.mark.asyncio
async def test_safe_reply_falls_back_to_plain_text_on_bad_request(mock_update):
    from telegram.error import BadRequest
    from services.telegram_bot.handlers import safe_reply

    # Configure reply_text to raise BadRequest on first call (with parse_mode="Markdown")
    # and succeed on second call (with parse_mode=None)
    mock_msg = mock_update.effective_message
    mock_msg.reply_text.side_effect = [
        BadRequest("Can't parse entities: can't find end of the entity starting at byte offset 144"),
        AsyncMock(message_id=999),
    ]

    await safe_reply(mock_msg, "Text with unclosed _entity")

    assert mock_msg.reply_text.call_count == 2
    # First call had Markdown
    assert mock_msg.reply_text.call_args_list[0][1]["parse_mode"] == "Markdown"
    # Fallback call had parse_mode=None
    assert mock_msg.reply_text.call_args_list[1][1]["parse_mode"] is None


@pytest.mark.asyncio
async def test_deny_command_success(mock_update, mock_context):
    mock_context.args = ["inc-20261021-0004", "Risk", "too", "high"]
    action_resp = MagicMock(status_code=200)
    action_resp.json.return_value = {"action_id": "act-demo-123"}

    approve_resp = MagicMock(status_code=200)
    approve_resp.json.return_value = {"status": "denied"}

    with patch("httpx.AsyncClient.get", return_value=action_resp), \
         patch("httpx.AsyncClient.post", return_value=approve_resp) as mock_post:
        await deny_command(mock_update, mock_context)

    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert payload["action_id"] == "act-demo-123"
    assert payload["decision"] == "denied"
    assert payload["reason"] == "Risk too high"

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Action Denied" in reply
    assert "inc-20261021-0004" in reply


@pytest.mark.asyncio
async def test_deny_command_no_pending_action(mock_update, mock_context):
    mock_context.args = ["inc-20261021-0004"]
    action_resp = MagicMock(status_code=404)

    with patch("httpx.AsyncClient.get", return_value=action_resp):
        await deny_command(mock_update, mock_context)

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "No pending action found" in reply


@pytest.mark.asyncio
async def test_approve_command_success(mock_update, mock_context):
    mock_context.args = ["inc-20261021-0004", "Approved", "by", "lead"]
    action_resp = MagicMock(status_code=200)
    action_resp.json.return_value = {"action_id": "act-demo-123"}

    approve_resp = MagicMock(status_code=200)
    approve_resp.json.return_value = {"status": "approved"}

    with patch("httpx.AsyncClient.get", return_value=action_resp), \
         patch("httpx.AsyncClient.post", return_value=approve_resp) as mock_post:
        await approve_command(mock_update, mock_context)

    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert payload["action_id"] == "act-demo-123"
    assert payload["decision"] == "approved"
    assert payload["reason"] == "Approved by lead"

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Action Approved" in reply
    assert "inc-20261021-0004" in reply


@pytest.mark.asyncio
async def test_approve_command_no_pending_action(mock_update, mock_context):
    mock_context.args = ["inc-20261021-0004"]
    action_resp = MagicMock(status_code=404)

    with patch("httpx.AsyncClient.get", return_value=action_resp):
        await approve_command(mock_update, mock_context)

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "No pending action found" in reply


@pytest.mark.asyncio
async def test_drop_command_success(mock_update, mock_context):
    mock_context.args = ["inc-20261021-0005", "False", "positive", "alert"]
    drop_resp = MagicMock(status_code=200)
    drop_resp.json.return_value = {"status": "dropped", "state": "CLOSED"}

    with patch("httpx.AsyncClient.post", return_value=drop_resp) as mock_post:
        await drop_command(mock_update, mock_context)

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "http://mock-api:8080/v1/incidents/inc-20261021-0005/drop"
    payload = mock_post.call_args[1]["json"]
    assert payload["reason"] == "False positive alert"
    assert payload["actor"] == "sre_alice"
    assert payload["telegram_user_id"] == "1001"

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Incident Dropped" in reply
    assert "inc-20261021-0005" in reply


@pytest.mark.asyncio
async def test_drop_command_no_args(mock_update, mock_context):
    mock_context.args = []
    await drop_command(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Usage: `/drop <incident_id> [reason]`" in reply


@pytest.mark.asyncio
async def test_approval_callback_missing_username_uses_full_name_not_unknown(mock_context):
    query = MagicMock()
    query.data = "approve:inc-01:act-01"
    query.from_user = MagicMock(id=1000000001, username=None, full_name="John Doe", first_name="John", last_name="Doe")
    query.message = MagicMock(text="Proposal")
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock(id=123456)

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"status": "approved", "actor": "john.doe"}
    with patch("httpx.AsyncClient.post", return_value=resp) as mock_post:
        await approval_callback(update, mock_context)

    payload = mock_post.call_args[1]["json"]
    assert "unknown" not in payload["actor"].lower()
    assert payload["actor"] == "John Doe"
    assert payload["telegram_user_id"] == "1000000001"
    assert payload["telegram_full_name"] == "John Doe"

    edited_text = query.edit_message_text.call_args[1]["text"]
    assert "Approved by john.doe" in edited_text



