"""Unit tests for TelegramNotifier (services/telegram_bot/notifier.py)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from services.telegram_bot.notifier import TelegramNotifier
from shared.models.actions import ActionPlanItem, ActionProposal
from shared.models.findings import EvidenceItem, MergedFinding


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=101))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=101))
    bot.send_document = AsyncMock(return_value=MagicMock(message_id=102))
    return bot


@pytest.fixture
def notifier(mock_bot):
    return TelegramNotifier(bot=mock_bot, default_chat_id="test-chat-123")


@pytest.mark.asyncio
async def test_send_incident_created_formatting(notifier, mock_bot):
    incident = {
        "incident_id": "inc-20261016-0001",
        "severity": "P1",
        "affected_service": "cartservice",
        "affected_namespace": "boutique",
        "created_at": "2026-10-16T00:00:00Z",
    }
    msg = await notifier.send_incident_created(incident)
    assert msg is not None
    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args[1]

    assert call_kwargs["chat_id"] == "test-chat-123"
    assert "🚨" in call_kwargs["text"]
    assert "inc-20261016-0001" in call_kwargs["text"]
    assert "P1" in call_kwargs["text"]
    assert "cartservice" in call_kwargs["text"]
    assert "boutique" in call_kwargs["text"]
    assert call_kwargs["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_incident_created_unconfigured_skips():
    empty_notifier = TelegramNotifier(bot=None, default_chat_id="")
    res = await empty_notifier.send_incident_created({"incident_id": "inc-1"})
    assert res is None


@pytest.mark.asyncio
async def test_send_triage_summary_top3_evidence(notifier, mock_bot):
    evidence_items = [
        EvidenceItem(source_tool="prometheus", query_or_cmd="sum(rate(http_requests[5m]))", raw_output=f"Metric spike #{i}").model_dump()
        for i in range(5)
    ]
    merged = MergedFinding(
        incident_id="inc-triage-01",
        primary_hypothesis="Pod OOM killed due to memory saturation.",
        merged_confidence=0.88,
        all_evidence=evidence_items,
    ).model_dump()

    incident = {"incident_id": "inc-triage-01", "affected_service": "cartservice"}
    msg = await notifier.send_triage_summary(incident, merged)

    assert msg is not None
    call_text = mock_bot.send_message.call_args[1]["text"]
    assert "Triage Summary: inc-triage-01" in call_text
    assert "Pod OOM killed due to memory saturation" in call_text
    assert "Metric spike #0" in call_text
    assert "Metric spike #1" in call_text
    assert "Metric spike #2" in call_text
    # 4th and 5th items must be capped out (top 3 only)
    assert "Metric spike #3" not in call_text
    assert "Metric spike #4" not in call_text


@pytest.mark.asyncio
async def test_send_approval_request_inline_keyboard(notifier, mock_bot):
    proposal = ActionProposal(
        incident_id="inc-act-01",
        confidence=0.92,
        risk_assessment="Low risk pod restart",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="cartservice-7b4d",
                parameters={"namespace": "boutique"},
                rationale="Clear memory leak",
            )
        ],
    ).model_dump()
    proposal["action_id"] = "act-test-01"

    incident = {"incident_id": "inc-act-01"}
    msg = await notifier.send_approval_request(incident, proposal)

    assert msg is not None
    assert "act-test-01" in notifier.tracked_actions
    assert notifier.tracked_actions["act-test-01"] == ("test-chat-123", 101)

    call_kwargs = mock_bot.send_message.call_args[1]
    assert "ACTION APPROVAL REQUIRED" in call_kwargs["text"]
    assert "restart_pod" in call_kwargs["text"]
    assert "0.92" in call_kwargs["text"]

    markup = call_kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 2
    row0 = markup.inline_keyboard[0]
    assert row0[0].text == "✅ Approve"
    assert row0[0].callback_data == "approve:inc-act-01:act-test-01"
    assert row0[1].text == "❌ Deny"
    assert row0[1].callback_data == "deny:inc-act-01:act-test-01"

    row1 = markup.inline_keyboard[1]
    assert row1[0].text == "🔄 Steer Plan"
    assert row1[0].callback_data == "steer:inc-act-01:act-test-01"


@pytest.mark.asyncio
async def test_send_denial_notification_edits_tracked_message(notifier, mock_bot):
    notifier.tracked_actions["act-test-01"] = ("test-chat-123", 777)
    notifier.action_texts["act-test-01"] = "Original Approval Request Header"

    incident = {"incident_id": "inc-act-01"}
    edited = await notifier.send_denial_notification(
        incident=incident,
        action_id="act-test-01",
        denied_by="oncall_eng",
        channel="dashboard",
        reason="Manual operator intervention",
    )

    assert edited is not None
    mock_bot.edit_message_text.assert_called_once()
    edit_kwargs = mock_bot.edit_message_text.call_args[1]

    assert edit_kwargs["chat_id"] == "test-chat-123"
    assert edit_kwargs["message_id"] == 777
    assert "Denied via Dashboard by oncall_eng" in edit_kwargs["text"]
    assert edit_kwargs["reply_markup"] is None  # Buttons removed


@pytest.mark.asyncio
async def test_send_denial_notification_untracked_sends_new_message(notifier, mock_bot):
    incident = {"incident_id": "inc-act-untracked"}
    msg = await notifier.send_denial_notification(
        incident=incident,
        action_id="act-untracked",
        denied_by="alice",
        channel="dashboard",
    )

    assert msg is not None
    mock_bot.send_message.assert_called_once()
    call_text = mock_bot.send_message.call_args[1]["text"]
    assert "Action Denied (Dashboard)" in call_text
    assert "alice" in call_text


@pytest.mark.asyncio
async def test_send_escalation(notifier, mock_bot):
    incident = {"incident_id": "inc-esc-01", "affected_service": "paymentservice"}
    msg = await notifier.send_escalation(incident, reason="Convergence timeout after 5m")

    assert msg is not None
    mock_bot.send_message.assert_called_once()
    text = mock_bot.send_message.call_args[1]["text"]
    assert "INCIDENT ESCALATED" in text
    assert "Convergence timeout after 5m" in text
    assert "/steer inc-esc-01" in text


@pytest.mark.asyncio
async def test_send_report_document_dispatch(notifier, mock_bot, tmp_path):
    report_file = tmp_path / "report.md"
    report_file.write_text("# Incident Report: inc-rep-01\n\nAll services recovered.")

    incident = {"incident_id": "inc-rep-01"}
    doc = await notifier.send_report(incident, str(report_file))

    assert doc is not None
    mock_bot.send_document.assert_called_once()
    call_kwargs = mock_bot.send_document.call_args[1]
    assert call_kwargs["chat_id"] == "test-chat-123"
    assert call_kwargs["filename"] == "report.md"
    assert "Post-Incident Report: inc-rep-01" in call_kwargs["caption"]


@pytest.mark.asyncio
async def test_dispatch_all_events(notifier):
    notifier.send_incident_created = AsyncMock(return_value=True)
    notifier.send_triage_summary = AsyncMock(return_value=True)
    notifier.send_approval_request = AsyncMock(return_value=True)
    notifier.send_denial_notification = AsyncMock(return_value=True)
    notifier.send_escalation = AsyncMock(return_value=True)
    notifier.send_report = AsyncMock(return_value=True)

    assert await notifier.dispatch("incident_created", "inc-1", {"incident": {}})
    notifier.send_incident_created.assert_called_once()

    assert await notifier.dispatch("triage_summary", "inc-1", {"incident": {}, "merged_findings": {}})
    notifier.send_triage_summary.assert_called_once()

    assert await notifier.dispatch("approval_request", "inc-1", {"incident": {}, "action": {}})
    notifier.send_approval_request.assert_called_once()

    assert await notifier.dispatch("denial_notification", "inc-1", {"action_id": "act-1", "actor": "bob"})
    notifier.send_denial_notification.assert_called_once()

    assert await notifier.dispatch("escalation", "inc-1", {"reason": "timeout"})
    notifier.send_escalation.assert_called_once()

    assert await notifier.dispatch("report_delivered", "inc-1", {"report_path": "/data/report.md"})
    notifier.send_report.assert_called_once()

    # Unknown event returns False
    assert not await notifier.dispatch("unknown_event", "inc-1", {})


@pytest.mark.asyncio
async def test_emit_record_consumed_by_notifier(notifier, mock_bot):
    from services.api_server.sag.approval import emit_approval_request
    from shared.models.actions import ActionProposal, ActionPlanItem

    mock_redis = AsyncMock()
    proposal = ActionProposal(
        incident_id="inc-prop-99",
        confidence=0.88,
        risk_assessment="Safe scale up",
        actions=[
            ActionPlanItem(
                tool_name="scale_deployment",
                target_resource="cartservice",
                parameters={"replicas": 3},
                rationale="Handle traffic spike",
            )
        ],
    )
    await emit_approval_request(
        mock_redis,
        incident_id="inc-prop-99",
        action_proposal=proposal,
        severity="P2",
        schedule_timeout=False,
    )
    raw = mock_redis.set.call_args_list[0][0][1]
    import json
    record = json.loads(raw)
    incident = {"incident_id": "inc-prop-99"}
    msg = await notifier.send_approval_request(incident, record)
    assert msg is not None
    call_kwargs = mock_bot.send_message.call_args[1]
    assert record["action_id"] in call_kwargs["text"]
    assert "scale_deployment" in call_kwargs["text"]
    assert "0.88" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_incident_created_payload_has_incident_key(notifier, mock_bot):
    payload = {
        "incident": {
            "incident_id": "inc-created-01",
            "severity": "P2",
            "affected_service": "paymentservice",
            "affected_namespace": "boutique",
            "created_at": "2026-10-20T10:00:00Z",
        }
    }
    await notifier.dispatch("incident_created", "inc-created-01", payload)
    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args[1]
    assert "inc-created-01" in call_kwargs["text"]
    assert "paymentservice" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_send_approval_request_includes_runbook_citations_and_rationale(notifier, mock_bot):
    from shared.models.actions import RunbookCitation

    citation = RunbookCitation(
        runbook_id="feature-flag-failure",
        book_name="Feature Flag Failure Mitigation",
        section="Remediation Steps",
        line_range="L46-L49",
        chunk_id="chunk 4/5",
        referenced_text="Call toggle_feature_flag with enabled: false and environment: otel-demo.",
    )
    proposal = ActionProposal(
        incident_id="inc-cit-tg-01",
        confidence=0.94,
        rationale=(
            "Disable active cartFailure flag per Feature Flag Failure Mitigation runbook.\n\n"
            "[Runbook Reference]\n"
            "id/title: 'feature-flag-failure/Feature Flag Failure Mitigation'\n"
            "section/line_range: 'Remediation Steps/L46-L49 (chunk 4/5)'"
        ),
        risk_assessment="Low risk reversible flag toggle",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable cartFailure flag.",
                runbook_citations=[citation],
            )
        ],
        runbook_citations=[citation],
    ).model_dump(mode="json")
    proposal["action_id"] = "act-cit-01"

    msg = await notifier.send_approval_request({"incident_id": "inc-cit-tg-01"}, proposal)
    assert msg is not None
    text = mock_bot.send_message.call_args[1]["text"]
    assert "• *Rationale:* Disable active cartFailure flag per Feature Flag Failure Mitigation runbook." in text
    assert "[Runbook Reference]" not in text
    assert "Runbook Citations:" in text
    assert "`id/title:` 'feature-flag-failure/Feature Flag Failure Mitigation'" in text
    assert "`section/line_range:` 'Remediation Steps/L46-L49 (chunk 4/5)'" in text
    assert "`reference:`" in text
    assert "toggle\\_feature\\_flag" in text or "toggle_feature_flag" in text

