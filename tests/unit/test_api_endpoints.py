import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from services.api_server.main import create_app
from shared.models.enums import IncidentState

@pytest.fixture
def mock_config(mocker):
    config_mock = mocker.patch("services.api_server.main.ConfigManager")
    config_mock.return_value.system.system.api_key = "test-key"
    config_mock.return_value.system.system.api_port = 8080
    config_mock.return_value.system.redis.url = "redis://fake"
    
    return config_mock

@pytest.fixture
def client(mock_config, mocker):
    mocker.patch("redis.from_url", return_value=mocker.Mock())
    app = create_app()
    # Mock api_key in middleware so the test client passes
    from services.api_server.middleware import APIKeyMiddleware
    
    with TestClient(app) as client:
        yield client

def test_health_returns_ok(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_health_no_auth_required(client):
    response = client.get("/v1/health", headers={})
    assert response.status_code == 200

def test_metrics_no_auth_required(client):
    response = client.get("/v1/metrics", headers={})
    assert response.status_code == 200

def test_alert_ingest_enqueues_only(client, mocker):
    payload = {
        "groupKey": "test",
        "alerts": [
            {
                "labels": {"severity": "P1", "service": "cart", "namespace": "prod"}
            }
        ]
    }
    client.app.state.redis.xadd.return_value = "1700000000000-0"
    response = client.post("/v1/alerts/ingest", json=payload, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "queued"
    assert res_data["stream_id"] == "1700000000000-0"
    xadd_calls = client.app.state.redis.xadd.call_args_list
    raw_calls = [c for c in xadd_calls if c[0][0] == "stream:alert:raw"]
    audit_calls = [c for c in xadd_calls if c[0][0] == "stream:audit:interactions"]
    assert len(raw_calls) == 1
    assert len(audit_calls) == 1
    import json
    assert json.loads(raw_calls[0][0][1]["payload"]) == payload
    assert raw_calls[0][0][1]["ingested_by"] == "admin"
    assert audit_calls[0][0][1]["category"] == "api_request"

def test_ingest_missing_api_key_returns_403(client):
    response = client.post("/v1/alerts/ingest", json={"alerts": []})
    assert response.status_code == 403

def test_ingest_invalid_api_key_returns_403(client):
    response = client.post("/v1/alerts/ingest", json={"alerts": []}, headers={"X-API-Key": "wrong"})
    assert response.status_code == 403

def test_transition_valid_event_advances_state(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "NEW"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    mock_machine.return_value.state = "GROUPING"
    from unittest.mock import AsyncMock
    mock_machine.return_value.trigger = AsyncMock(return_value=True)
    
    req = {"event": "alert_received", "actor": "system", "metadata": {}}
    response = client.post("/v1/incidents/inc-1/transition", json=req, headers={"X-API-Key": "test-key"})
    
    assert response.status_code == 200
    assert response.json()["to"] == "GROUPING"

def test_transition_invalid_event_returns_400(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "NEW"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    import asyncio
    async def fake_trigger(*args, **kwargs):
        raise ValueError("Invalid event")
    mock_machine.return_value.trigger = fake_trigger
    
    req = {"event": "triage_complete", "actor": "system", "metadata": {}}
    response = client.post("/v1/incidents/inc-1/transition", json=req, headers={"X-API-Key": "test-key"})
    
    assert response.status_code == 400

def test_transition_terminal_state_returns_400(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "CLOSED"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    import asyncio
    async def fake_trigger(*args, **kwargs):
        raise ValueError("Cannot transition from terminal state")
    mock_machine.return_value.trigger = fake_trigger
    
    req = {"event": "escalate", "actor": "system", "metadata": {}}
    response = client.post("/v1/incidents/inc-1/transition", json=req, headers={"X-API-Key": "test-key"})
    
    assert response.status_code == 400

def test_get_incident_returns_detail(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "NEW", "incident_id": "inc-1"})
    response = client.get("/v1/incidents/inc-1", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["incident_id"] == "inc-1"

def test_get_ledger_returns_entries(client, mocker):
    mock_get = mocker.patch("shared.ledger.writer.get_ledger_entries")
    
    entry = mocker.Mock()
    entry.model_dump.return_value = {"entry_id": "e1", "state": "NEW"}
    mock_get.return_value = [entry]
    
    response = client.get("/v1/incidents/inc-1/ledger", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["entry_id"] == "e1"

def test_get_incidents_list(client, mocker):
    mocker.patch("services.api_server.routers.incidents.list_incidents", return_value=[{"incident_id": "inc-1"}])
    response = client.get("/v1/incidents", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert len(response.json()) == 1

def test_config_get_redacts_api_key(client, mocker):
    config_mock = mocker.patch("services.api_server.routers.config_routes.ConfigManager")
    config_mock.return_value.system.model_dump.return_value = {"system": {"api_key": "secret"}}
    
    response = client.get("/v1/config", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["system"]["api_key"] == "***"

def test_config_mcp_lists_servers(client, mocker):
    config_mock = mocker.patch("services.api_server.routers.config_routes.ConfigManager")
    config_mock.return_value.mcp.model_dump.return_value = {"servers": {"github": {}}}
    
    response = client.get("/v1/config/mcp", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert "github" in response.json()["servers"]

def test_escalate_endpoint(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "NEW"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    from unittest.mock import AsyncMock
    mock_machine.return_value.trigger = AsyncMock(return_value=True)
    
    response = client.post(
        "/v1/incidents/inc-1/escalate",
        json={"reason": "On-call handoff requested", "actor": "alice", "channel": "telegram"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "escalated"
    mock_machine.return_value.trigger.assert_awaited_once_with(
        "escalate", "alice", {"reason": "On-call handoff requested", "channel": "telegram"}
    )

def test_close_endpoint(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "REPORTING"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    mock_machine.return_value.state = "REPORTING"
    from unittest.mock import AsyncMock
    mock_machine.return_value.trigger = AsyncMock(return_value=True)
    
    response = client.post("/v1/incidents/inc-1/close", json={"reason": "test"}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["status"] == "closed"

def test_close_endpoint_with_generate_report(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"state": "ESCALATED"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    mock_machine.return_value.state = "ESCALATED"
    from unittest.mock import AsyncMock
    mock_machine.return_value.trigger = AsyncMock(return_value=True)
    
    response = client.post(
        "/v1/incidents/inc-1/close",
        json={"reason": "Operator resolved and requested report", "generate_report": True},
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "reporting"
    mock_machine.return_value.trigger.assert_called_once_with(
        "generate_report", "operator", {"reason": "Operator resolved and requested report", "channel": "dashboard"}
    )

def test_metrics_contains_expected_metric(client):
    response = client.get("/v1/metrics")
    assert response.status_code == 200
    assert "aegis_incident_state_transitions_total" in response.text

def test_drop_endpoint_success(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"incident_id": "inc-1", "state": "REMEDIATION"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    mock_machine.return_value.state = "REMEDIATION"
    from unittest.mock import AsyncMock
    mock_machine.return_value.trigger = AsyncMock(return_value=True)

    response = client.post(
        "/v1/incidents/inc-1/drop",
        json={"reason": "False positive test", "actor": "test_sre"},
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "dropped"
    assert response.json()["incident_id"] == "inc-1"
    mock_machine.return_value.trigger.assert_awaited_once_with(
        "drop", "test_sre", {"reason": "False positive test", "action": "dropped", "channel": "dashboard"}
    )

def test_drop_endpoint_already_closed(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"incident_id": "inc-1", "state": "CLOSED"})
    mock_machine = mocker.patch("services.api_server.routers.incidents.load_machine")
    mock_machine.return_value.state = "CLOSED"

    response = client.post(
        "/v1/incidents/inc-1/drop",
        json={"reason": "Already closed"},
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 400
    assert "already closed" in response.json()["detail"]


def test_get_incident_report_found(client, tmp_path, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"incident_id": "inc-1", "state": "RESOLVED"})
    report_file = tmp_path / "report.md"
    report_file.write_text("# Incident Report\nContent here", encoding="utf-8")
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.is_file", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value="# Incident Report\nContent here")

    response = client.get(
        "/v1/incidents/inc-1/report",
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["incident_id"] == "inc-1"
    assert data["exists"] is True
    assert "# Incident Report" in data["content"]


def test_get_incident_report_not_found(client, mocker):
    mocker.patch("services.api_server.routers.incidents.get_incident", return_value={"incident_id": "inc-2", "state": "TRIAGE"})
    mocker.patch("pathlib.Path.is_file", return_value=False)

    response = client.get(
        "/v1/incidents/inc-2/report",
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["incident_id"] == "inc-2"
    assert data["exists"] is False
    assert data["content"] is None

