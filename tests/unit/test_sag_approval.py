import json
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from services.api_server.main import create_app
from services.api_server.incident_store import create_incident
from services.api_server.sag.approval import (
    emit_approval_request,
    handle_approval_timeout,
    record_denial_notification,
)
from services.api_server.state_machine import load_machine
from shared.models.actions import ActionProposal, ActionPlanItem
from shared.models.alerts import Alert, AlertGroup
from shared.models.enums import IncidentState
from tests.unit.test_correlator import CorrelatorFakeRedis


def _create_test_proposal(incident_id: str) -> ActionProposal:
    return ActionProposal(
        incident_id=incident_id,
        confidence=0.85,
        risk_assessment="Low risk pod restart",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="cartservice",
                parameters={"namespace": "boutique", "pod_name": "cartservice-xxx"},
                rationale="Clear memory leak",
            )
        ],
    )


def _seed_incident_in_state(fake_redis, incident_id: str, state: str = "AWAITING_APPROVAL"):
    alert = Alert(
        fingerprint="fp-001",
        alertname="HighMemoryUsage",
        status="firing",
        severity="P2",
        service="cartservice",
        namespace="boutique",
        starts_at=datetime.now(timezone.utc),
    )
    group = AlertGroup(
        group_id="grp-001",
        primary_alert=alert,
        related_alerts=[],
        severity="P2",
        affected_service="cartservice",
        affected_namespace="boutique",
    )
    create_incident(fake_redis, incident_id, group)
    # Update incident state in redis store
    raw = fake_redis.get(f"incident:{incident_id}:state")
    inc_data = json.loads(raw)
    inc_data["state"] = state
    fake_redis.set(f"incident:{incident_id}:state", json.dumps(inc_data))


@pytest.mark.asyncio
async def test_emit_approval_request_writes_redis():
    fake_redis = CorrelatorFakeRedis()
    proposal = _create_test_proposal("inc-101")

    req = await emit_approval_request(
        redis_client=fake_redis,
        incident_id="inc-101",
        action_proposal=proposal,
        severity="P2",
        timeout_seconds=900,
        action_id="act-001",
    )

    assert req.request_id == "act-001"
    assert req.incident_id == "inc-101"
    assert req.expires_at is not None

    raw = fake_redis.get("incident:inc-101:action:act-001")
    assert raw is not None
    record = json.loads(raw)
    assert record["status"] == "pending"
    assert record["action_id"] == "act-001"
    assert record["timeout_seconds"] == 900


@pytest.mark.asyncio
async def test_emit_approval_request_severity_timeouts():
    fake_redis = CorrelatorFakeRedis()
    proposal = _create_test_proposal("inc-102")

    # P1: 300s
    req_p1 = await emit_approval_request(fake_redis, "inc-102", proposal, severity="P1", action_id="act-p1")
    rec_p1 = json.loads(fake_redis.get("incident:inc-102:action:act-p1"))
    assert rec_p1["timeout_seconds"] == 300

    # P2: 900s
    req_p2 = await emit_approval_request(fake_redis, "inc-102", proposal, severity="P2", action_id="act-p2")
    rec_p2 = json.loads(fake_redis.get("incident:inc-102:action:act-p2"))
    assert rec_p2["timeout_seconds"] == 900

    # P3: 1800s
    req_p3 = await emit_approval_request(fake_redis, "inc-102", proposal, severity="P3", action_id="act-p3")
    rec_p3 = json.loads(fake_redis.get("incident:inc-102:action:act-p3"))
    assert rec_p3["timeout_seconds"] == 1800


@pytest.mark.asyncio
async def test_handle_approval_timeout_escalates():
    fake_redis = CorrelatorFakeRedis()
    incident_id = "inc-timeout-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    proposal = _create_test_proposal(incident_id)
    await emit_approval_request(fake_redis, incident_id, proposal, timeout_seconds=10, action_id="act-to")

    # Run timeout handler with small sleep
    await handle_approval_timeout(fake_redis, incident_id, "act-to", timeout_seconds=0.01)

    rec = json.loads(fake_redis.get(f"incident:{incident_id}:action:act-to"))
    assert rec["status"] == "timed_out"

    # State machine should have escalated
    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "ESCALATED"


def test_record_denial_notification():
    fake_redis = CorrelatorFakeRedis()
    notif = record_denial_notification(
        redis_client=fake_redis,
        incident_id="inc-103",
        action_id="act-003",
        actor="oncall_eng",
        channel="telegram",
        reason="high blast radius",
    )
    assert notif["target_channel"] == "dashboard"
    assert "was denied by oncall_eng via telegram" in notif["message"]

    raw = fake_redis.get("incident:inc-103:denial_notification")
    assert raw is not None
    saved = json.loads(raw)
    assert saved["target_channel"] == "dashboard"


def test_approve_endpoint_approves(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-appr-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Seed pending action
    fake_redis.set(
        f"incident:{incident_id}:action:act-001",
        json.dumps({"action_id": "act-001", "status": "pending"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-001",
                "decision": "approved",
                "actor": "operator@example.com",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"

    # Verify Redis status is approved and decided_by is bound to authenticated API key owner
    rec = json.loads(fake_redis.get(f"incident:{incident_id}:action:act-001"))
    assert rec["status"] == "approved"
    assert rec["decided_by"] == "admin"

    # Verify state machine reached EXECUTING
    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "EXECUTING"


def test_approve_endpoint_denies(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-deny-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Seed pending action
    fake_redis.set(
        f"incident:{incident_id}:action:act-002",
        json.dumps({"action_id": "act-002", "status": "pending"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-002",
                "decision": "denied",
                "actor": "lead@example.com",
                "channel": "telegram",
                "reason": "Not verified yet",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "denied"

    # Verify Redis status is denied
    rec = json.loads(fake_redis.get(f"incident:{incident_id}:action:act-002"))
    assert rec["status"] == "denied"

    # Verify state machine reached ESCALATED
    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "ESCALATED"

    # Verify denial notification was stored for dashboard
    notif_raw = fake_redis.get(f"incident:{incident_id}:denial_notification")
    assert notif_raw is not None
    notif = json.loads(notif_raw)
    assert notif["target_channel"] == "dashboard"


def test_approve_endpoint_idempotent_409(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-idem-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Already denied action (denial is final)
    fake_redis.set(
        f"incident:{incident_id}:action:act-done",
        json.dumps({"action_id": "act-done", "status": "denied"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-done",
                "decision": "denied",
                "actor": "telegram_user",
                "channel": "telegram",
            },
        )
        assert resp.status_code == 409
        assert "already been decided" in resp.json()["detail"]


def test_approve_endpoint_unknown_action_404(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-404-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-nonexistent",
                "decision": "approved",
                "actor": "user",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 404


def test_approve_endpoint_invalid_decision_400(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-400-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-001",
                "decision": "maybe",
                "actor": "user",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 400
        assert "Decision must be 'approved', 'denied', or 'steer'" in resp.json()["detail"]


def test_approve_endpoint_steer_success(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-steer-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Seed pending action
    fake_redis.set(
        f"incident:{incident_id}:action:act-003",
        json.dumps({"action_id": "act-003", "status": "pending"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-003",
                "decision": "steer",
                "actor": "sre_lead@example.com",
                "channel": "telegram",
                "instruction": "Scale replicas instead of restarting pod",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "steered"
        assert "superseded by steering instruction" in data["message"]

    # Verify Redis action status is steered
    rec = json.loads(fake_redis.get(f"incident:{incident_id}:action:act-003"))
    assert rec["status"] == "steered"
    assert rec["instruction"] == "Scale replicas instead of restarting pod"

    # Verify state machine reached REMEDIATION
    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "REMEDIATION"

    # Verify steering instructions are recorded
    raw_instructions = fake_redis.get(f"incident:{incident_id}:steering")
    assert raw_instructions is not None
    steer_data = json.loads(raw_instructions)
    assert steer_data["actor"] == "sre_lead@example.com"
    assert steer_data["instruction"] == "Scale replicas instead of restarting pod"
    assert steer_data["channel"] == "telegram"


def test_approve_endpoint_steer_missing_instruction_400(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-steer-missing"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-001",
                "decision": "steer",
                "actor": "user",
                "channel": "dashboard",
                "instruction": "",
            },
        )
        assert resp.status_code == 400
        assert "instruction is required" in resp.json()["detail"]


def test_steer_endpoint_resumes_from_escalated(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-esc-steer-01"
    # Aegis had halted/escalated
    _seed_incident_in_state(fake_redis, incident_id, state="ESCALATED")

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/steer",
            json={
                "actor": "commander@example.com",
                "channel": "telegram",
                "instruction": "Halting cleared. Apply safe traffic drain on checkout service.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "steered"
        assert data["state"] == "REMEDIATION"

    # Verify state machine resumed to REMEDIATION
    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "REMEDIATION"

    # Verify steering instructions recorded
    with TestClient(app) as client:
        client.headers.update(headers)
        get_resp = client.get(f"/v1/incidents/{incident_id}/steering")
        assert get_resp.status_code == 200
        steering_list = get_resp.json()
        assert len(steering_list) == 1
        assert steering_list[0]["actor"] == "commander@example.com"
        assert steering_list[0]["channel"] == "telegram"
        assert "traffic drain" in steering_list[0]["instruction"]


def test_transition_action_proposed_emits_pending_record(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-prop-trans-01"
    _seed_incident_in_state(fake_redis, incident_id, state="REMEDIATION")

    proposal = _create_test_proposal(incident_id)
    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/transition",
            json={
                "event": "action_proposed",
                "actor": "phase2_worker",
                "metadata": {"proposal": proposal.model_dump(mode="json")},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["to"] == "AWAITING_APPROVAL"

    # Verify pending action record exists in Redis
    keys = [k for k in fake_redis.store.keys() if k.startswith(f"incident:{incident_id}:action:act-")]
    assert len(keys) == 1
    raw = fake_redis.get(keys[0])
    assert raw is not None
    record = json.loads(raw)
    assert record["status"] == "pending"
    assert "expires_at" in record


def test_approval_does_not_fall_back_to_alias(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-alias-test-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Only set alias key, but request specific unknown action_id
    fake_redis.set(
        f"incident:{incident_id}:action",
        json.dumps({"status": "approved", "action_id": "act-alias"}),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": "act-specific-999",
                "decision": "approved",
                "actor": "commander",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


def test_deny_after_approved_escalates_when_executing(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-deny-executing-01"
    # Incident was approved and is in EXECUTING
    _seed_incident_in_state(fake_redis, incident_id, state="EXECUTING")
    act_id = "act-exec-001"
    fake_redis.set(
        f"incident:{incident_id}:action:{act_id}",
        json.dumps({
            "action_id": act_id,
            "status": "approved",
            "incident_id": incident_id,
            "proposal": {"actions": [{"tool_name": "restart_pod"}]},
        }),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": act_id,
                "decision": "denied",
                "actor": "commander",
                "channel": "dashboard",
                "reason": "Emergency halt",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "denied"

    # Verify state machine escalated!
    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "ESCALATED"


def test_approve_when_not_awaiting_approval_409_and_record_unchanged(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-not-awaiting-01"
    # State is REMEDIATION (not AWAITING_APPROVAL)
    _seed_incident_in_state(fake_redis, incident_id, state="REMEDIATION")
    act_id = "act-pending-001"
    record_initial = {
        "action_id": act_id,
        "status": "pending",
        "incident_id": incident_id,
        "proposal": {"actions": [{"tool_name": "restart_pod"}]},
    }
    fake_redis.set(f"incident:{incident_id}:action:{act_id}", json.dumps(record_initial))

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": act_id,
                "decision": "approved",
                "actor": "commander",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 409
        assert "cannot approve" in resp.json()["detail"]

    # Record must still be pending and un-mutated in Redis
    raw = fake_redis.get(f"incident:{incident_id}:action:{act_id}")
    saved = json.loads(raw)
    assert saved["status"] == "pending"


def test_approve_on_timed_out_409(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-timeout-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")
    act_id = "act-to-001"
    fake_redis.set(
        f"incident:{incident_id}:action:{act_id}",
        json.dumps({
            "action_id": act_id,
            "status": "timed_out",
            "incident_id": incident_id,
            "proposal": {"actions": [{"tool_name": "restart_pod"}]},
        }),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": act_id,
                "decision": "approved",
                "actor": "commander",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 409
        assert "already been decided" in resp.json()["detail"]


def test_deny_on_timed_out_allowed(mocker):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-timeout-deny-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")
    act_id = "act-to-002"
    fake_redis.set(
        f"incident:{incident_id}:action:{act_id}",
        json.dumps({
            "action_id": act_id,
            "status": "timed_out",
            "incident_id": incident_id,
            "proposal": {"actions": [{"tool_name": "restart_pod"}]},
        }),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={
                "action_id": act_id,
                "decision": "denied",
                "actor": "commander",
                "channel": "dashboard",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"


@pytest.mark.asyncio
async def test_timeout_missing_record_escalates():
    fake_redis = CorrelatorFakeRedis()
    incident_id = "inc-missing-rec-01"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")

    # Run handle_approval_timeout with non-existent action_id and 0 timeout
    await handle_approval_timeout(
        redis_client=fake_redis,
        incident_id=incident_id,
        action_id="act-nonexistent",
        timeout_seconds=0,
    )

    machine = load_machine(incident_id, fake_redis)
    assert machine.state == "ESCALATED"


@pytest.mark.asyncio
async def test_startup_sweep_rearms_pending():
    from services.api_server.sag.approval import rearm_pending_approval_timers

    fake_redis = CorrelatorFakeRedis()
    # 1. Incident with pending unexpired action
    inc_1 = "inc-sweep-01"
    _seed_incident_in_state(fake_redis, inc_1, state="AWAITING_APPROVAL")
    act_1 = "act-sweep-01"
    future_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    fake_redis.set(
        f"incident:{inc_1}:action:{act_1}",
        json.dumps({
            "action_id": act_1,
            "status": "pending",
            "expires_at": future_time.isoformat(),
        }),
    )
    fake_redis.rpush(f"incident:{inc_1}:pending_actions", act_1)

    # 2. Incident with pending expired action
    inc_2 = "inc-sweep-02"
    _seed_incident_in_state(fake_redis, inc_2, state="AWAITING_APPROVAL")
    act_2 = "act-sweep-02"
    past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    fake_redis.set(
        f"incident:{inc_2}:action:{act_2}",
        json.dumps({
            "action_id": act_2,
            "status": "pending",
            "expires_at": past_time.isoformat(),
        }),
    )
    fake_redis.rpush(f"incident:{inc_2}:pending_actions", act_2)

    count = await rearm_pending_approval_timers(fake_redis)
    assert count == 1  # 1 unexpired re-armed

    # inc_2 must be marked timed_out and state escalated
    raw_2 = fake_redis.get(f"incident:{inc_2}:action:{act_2}")
    rec_2 = json.loads(raw_2)
    assert rec_2["status"] == "timed_out"
    machine_2 = load_machine(inc_2, fake_redis)
    assert machine_2.state == "ESCALATED"


@pytest.mark.parametrize(
    "current_status,decision,expected_code",
    [
        ("pending", "approved", 200),
        ("pending", "denied", 200),
        ("pending", "steer", 200),
        ("approved", "approved", 409),  # double-tap
        ("approved", "denied", 200),    # deny wins over approved!
        ("approved", "steer", 200),     # steer after approve
        ("denied", "approved", 409),    # deny wins: denied action cannot be re-authorized
        ("denied", "denied", 409),      # already denied
        ("denied", "steer", 409),       # cannot steer denied
        ("steered", "approved", 409),   # cannot approve steered
        ("steered", "denied", 200),     # deny can block steered
        ("steered", "steer", 409),      # cannot re-steer already steered
        ("timed_out", "approved", 409), # cannot approve timed out
        ("timed_out", "denied", 200),   # deny allowed on timed out
        ("timed_out", "steer", 409),    # cannot steer timed out
    ],
)
def test_approve_decision_matrix(mocker, current_status, decision, expected_code):
    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = f"inc-matrix-{current_status}-{decision}"
    _seed_incident_in_state(fake_redis, incident_id, state="AWAITING_APPROVAL")
    act_id = f"act-matrix-{current_status}-{decision}"
    fake_redis.set(
        f"incident:{incident_id}:action:{act_id}",
        json.dumps({
            "action_id": act_id,
            "status": current_status,
            "incident_id": incident_id,
            "proposal": {"actions": [{"tool_name": "restart_pod"}]},
        }),
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        payload = {
            "action_id": act_id,
            "decision": decision,
            "actor": "commander",
            "channel": "dashboard",
        }
        if decision == "steer":
            payload["instruction"] = "Try scaling instead"

        resp = client.post(f"/v1/incidents/{incident_id}/approve", json=payload)
        assert resp.status_code == expected_code, f"Expected {expected_code} for {current_status} x {decision}, got {resp.status_code}: {resp.text}"


def test_transition_action_proposed_persists_runbook_citations_in_ledger_and_action_record(mocker):
    from shared.ledger.writer import get_ledger_entries
    from shared.models.actions import RunbookCitation

    fake_redis = CorrelatorFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    app = create_app()
    app.state.redis = fake_redis
    incident_id = "inc-cit-ledger-1"
    _seed_incident_in_state(fake_redis, incident_id, state="REMEDIATION")

    cit = RunbookCitation(
        runbook_id="oom-killed-pod",
        book_name="OOMKilled Pod Remediation",
        section="Remediation Steps",
        line_range="L18-L24",
        chunk_id="chunk 2/4",
        referenced_text="Restart pod via restart_pod then verify convergence.",
    )
    prop = ActionProposal(
        incident_id=incident_id,
        rationale="Restart pod per OOMKilled Pod Remediation runbook.",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice",
                parameters={"namespace": "boutique", "pod_name": "cartservice-0"},
                rationale="Restart pod [Runbook Ref: OOMKilled Pod Remediation, L18-L24]",
                runbook_citations=[cit],
            )
        ],
        runbook_citations=[cit],
        confidence=0.91,
        risk_assessment="Low risk",
    )

    headers = {"X-API-Key": app.state.config.system.system.api_key}
    with TestClient(app) as client:
        client.headers.update(headers)
        resp = client.post(
            f"/v1/incidents/{incident_id}/transition",
            json={
                "event": "action_proposed",
                "actor": "phase2_worker",
                "metadata": {
                    "action_id": "act-cit-ledger-1",
                    "proposal": prop.model_dump(mode="json"),
                    "pre_approved": False,
                },
            },
        )
        assert resp.status_code == 200, resp.text

        act_resp = client.get(f"/v1/incidents/{incident_id}/action")
        assert act_resp.status_code == 200
        act_data = act_resp.json()
        assert act_data["runbook_citations"][0]["line_range"] == "L18-L24"
        assert act_data["proposal"]["runbook_citations"][0]["book_name"] == "OOMKilled Pod Remediation"

    entries = get_ledger_entries(fake_redis, incident_id)
    proposed_entry = next(e for e in entries if e.event_type == "action_proposed")
    assert proposed_entry.details["runbook_citations"][0]["line_range"] == "L18-L24"
    assert proposed_entry.details["runbook_citations"][0]["chunk_id"] == "chunk 2/4"



