import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, AsyncMock, patch

from services.api_server.main import create_app
from shared.models.enums import IncidentState
from shared.harness.sag_guard import SAGGuard, SAGActionBlockedError


import redis

@pytest.fixture
def mock_redis():
    class FakeRedis(redis.Redis):
        def __init__(self):
            self.storage = {}

        def get(self, key):
            val = self.storage.get(key)
            if isinstance(val, str):
                return val.encode('utf-8')
            return val

        def set(self, key, value, **kwargs):
            if isinstance(value, bytes):
                value = value.decode('utf-8')
            self.storage[key] = value
            return True

        def rpush(self, key, *values):
            if key not in self.storage:
                self.storage[key] = []
            if isinstance(self.storage[key], list):
                self.storage[key].extend(values)
            return len(self.storage[key])

        def xadd(self, stream, fields, **kwargs):
            return "1000-0"

        def ping(self):
            return True

        def close(self):
            pass

        def scan_iter(self, match):
            return []

    return FakeRedis()


@pytest.fixture
def client(mock_redis, mocker):
    mock_cfg = mocker.patch("services.api_server.main.ConfigManager")
    mock_cfg.return_value.system.system.api_key = "test-key"
    mock_cfg.return_value.system.system.api_port = 8080
    mock_cfg.return_value.system.redis.url = "redis://mock"

    app = create_app()
    app.state.redis = mock_redis
    return TestClient(app)


def test_cors_options_preflight_allowed_without_api_key(client):
    """Test that CORS preflight OPTIONS requests bypass API key middleware."""
    response = client.options(
        "/v1/incidents",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" in response.headers


def test_system_pause_and_resume_lifecycle(client, mock_redis):
    """Test pausing and resuming the system globally."""
    headers = {"X-API-Key": "test-key"}

    # 1. Check initial status
    resp = client.get("/v1/system/status", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "operational"
    assert resp.json()["paused"] is False

    # 2. Call pause with valid test key
    pause_resp = client.post(
        "/v1/system/pause",
        json={"reason": "Drill maintenance", "actor": "sre-lead"},
        headers=headers,
    )
    assert pause_resp.status_code == 200
    assert pause_resp.json()["status"] == "paused"
    assert pause_resp.json()["paused"] is True
    assert pause_resp.json()["reason"] == "Drill maintenance"

    # Check status reflects pause
    stat_resp = client.get("/v1/system/status", headers=headers)
    assert stat_resp.status_code == 200
    assert stat_resp.json()["paused"] is True
    assert stat_resp.json()["status"] == "paused"

    # 3. Resume
    res_resp = client.post(
        "/v1/system/resume",
        json={"actor": "sre-lead"},
        headers=headers,
    )
    assert res_resp.status_code == 200
    assert res_resp.json()["status"] == "operational"
    assert res_resp.json()["paused"] is False


def test_incident_pause_and_resume_endpoints(client, mock_redis):
    """Test pausing an incident into STANDBY and resuming it."""
    headers = {"X-API-Key": "test-key"}
    incident_id = "inc-20261021-0001"

    init_state = {
        "incident_id": incident_id,
        "state": "REMEDIATION",
        "severity": "P2",
        "affected_service": "cartservice",
    }
    mock_redis.storage[f"incident:{incident_id}:state"] = json.dumps(init_state)

    # 1. Pause incident
    pause_resp = client.post(
        f"/v1/incidents/{incident_id}/pause",
        json={"reason": "Operator manual inspection"},
        headers=headers,
    )
    assert pause_resp.status_code == 200
    assert pause_resp.json()["status"] == "paused"
    assert pause_resp.json()["state"] == "STANDBY"

    # 2. Resume incident with steering guidance
    resume_resp = client.post(
        f"/v1/incidents/{incident_id}/resume",
        json={"instruction": "Scale deployment to 4 replicas", "actor": "sre-lead"},
        headers=headers,
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["status"] == "resumed"
    assert resume_resp.json()["state"] == "REMEDIATION"

    # Verify steering was recorded
    steer_raw = mock_redis.storage.get(f"incident:{incident_id}:steering")
    assert steer_raw is not None
    assert "Scale deployment to 4 replicas" in str(steer_raw)


def test_config_endpoints_agents_and_sag(client):
    """Test GET /v1/config/agents, GET /v1/config/sag, and PUT sag tools."""
    headers = {"X-API-Key": "test-key"}

    # GET agents
    agents_resp = client.get("/v1/config/agents", headers=headers)
    assert agents_resp.status_code == 200
    assert "agents" in agents_resp.json()

    # GET sag
    sag_resp = client.get("/v1/config/sag", headers=headers)
    assert sag_resp.status_code == 200
    assert "tools" in sag_resp.json()

    # PUT sag tool policy
    try:
        put_resp = client.put("/v1/config/sag/tools/restart_pod", json={"pre_approved": True}, headers=headers)
        assert put_resp.status_code == 200
        assert put_resp.json()["status"] == "updated"
        assert put_resp.json()["tool_name"] == "restart_pod"
    finally:
        client.put("/v1/config/sag/tools/restart_pod", json={"pre_approved": False}, headers=headers)


@pytest.mark.asyncio
async def test_sag_guard_fails_closed_when_system_paused(mock_redis):
    """Verify that SAGGuard raises SAGActionBlockedError when system is paused globally."""
    # Set system status to paused
    mock_redis.storage["system:status:paused"] = json.dumps({
        "paused": True,
        "reason": "Emergency maintenance window",
    })

    guard = SAGGuard(mcp_client=Mock(), redis_client=mock_redis)

    with pytest.raises(SAGActionBlockedError) as exc_info:
        await guard.pre_tool_call(
            tool_name="restart_pod",
            arguments={"pod_name": "cartservice-1", "namespace": "boutique"},
            incident_id="inc-test-01",
            action_id="act-test-01",
        )
    assert "System is in global standby" in str(exc_info.value)
