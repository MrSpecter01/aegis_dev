"""Unit tests for multi-entity API keys, dual-port auth, Telegram attribution, audit logging, and history cap."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
import redis

from services.api_server.ingestion.consumer import run_alert_consumer
from services.api_server.main import create_app
from services.api_server.routers import config_routes
from services.mcp.tools.prometheus import prometheus_query
from services.mcp.tools.loki import loki_query
from services.mcp.tools.tempo import tempo_search
from services.mcp.tools.jaeger import jaeger_search_traces
from services.mcp.tools.featureflags import get_feature_flag
from shared.audit.tracker import (
    AUDIT_STREAM_KEY,
    list_interactions,
    reset_audit_context,
    set_audit_context,
    set_audit_redis_client,
)
from shared.auth.registry import IdentityRegistry
from shared.config.loader import load_identities_config
from shared.config.schemas import (
    ExternalClientIdentityConfig,
    IdentitiesConfig,
    MCPServerConfig,
    OperatorIdentityConfig,
    WebhookIdentityConfig,
)
from shared.harness.mcp import MCPClient
from shared.models.ledger import LedgerEntry
from tests.unit.test_correlator import CorrelatorFakeRedis


class AuthAuditFakeRedis(CorrelatorFakeRedis):
    def __init__(self):
        super().__init__()
        self.streams = {}
        self.groups = set()
        self.acked = []
        self._seq = 0

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if (stream, group) in self.groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.add((stream, group))

    def xreadgroup(self, group, consumer, streams, count=10, block=1000):
        stream_key = next(iter(streams.keys()))
        msgs = list(self.streams.get(stream_key, []))
        self.streams[stream_key] = []
        return [(stream_key, msgs)] if msgs else []

    def xack(self, stream, group, msg_id):
        self.acked.append((stream, group, msg_id))

    def xadd(self, stream, fields, maxlen=None, approximate=False):
        self._seq += 1
        msg_id = f"170000000-{self._seq}"
        self.streams.setdefault(stream, []).append((msg_id, fields))
        if maxlen is not None and len(self.streams[stream]) > maxlen:
            self.streams[stream] = self.streams[stream][-maxlen:]
        return msg_id

    def xrevrange(self, stream, max="+", min="-", count=100):
        return list(reversed(self.streams.get(stream, [])))[:count]

    def ping(self):
        return True


def _build_identities_cfg() -> IdentitiesConfig:
    return IdentitiesConfig(
        operators=[
            OperatorIdentityConfig(
                id="op-alice",
                username="alice",
                display_name="Alice SRE",
                role="admin",
                api_key_env="TEST_ALICE_KEY",
                telegram_user_id="1000000001",
            ),
            OperatorIdentityConfig(
                id="op-bob",
                username="bob",
                display_name="Bob OnCall",
                role="operator",
                api_key_env="TEST_BOB_KEY",
                telegram_user_id="999000111",
            ),
        ],
        webhooks=[
            WebhookIdentityConfig(
                id="wh-am-prod",
                name="alertmanager-prod",
                source_type="alertmanager",
                api_key_env="TEST_WEBHOOK_KEY",
                scopes=["alerts:ingest"],
            )
        ],
        external_clients=[
            ExternalClientIdentityConfig(
                id="ext-reader",
                name="grafana-exporter",
                api_key_env="TEST_CLIENT_READ_KEY",
                scopes=["incidents:read", "audit:read"],
            )
        ],
    )


def _setup_dual_clients(monkeypatch, mocker):
    monkeypatch.setenv("TEST_ALICE_KEY", "alice-secret-key")
    monkeypatch.setenv("TEST_BOB_KEY", "bob-secret-key")
    monkeypatch.setenv("TEST_WEBHOOK_KEY", "webhook-secret-key")
    monkeypatch.setenv("TEST_CLIENT_READ_KEY", "client-read-key")

    mock_cfg = mocker.patch("services.api_server.main.ConfigManager")
    mock_cfg.return_value.system.system.api_key = "legacy-secret"
    mock_cfg.return_value.system.system.api_port = 8080
    mock_cfg.return_value.system.system.internal_api_port = 8081
    mock_cfg.return_value.system.redis.url = "redis://fake"
    mock_cfg.return_value.identities = _build_identities_cfg()

    fake_redis = AuthAuditFakeRedis()
    mocker.patch("redis.from_url", return_value=fake_redis)

    ext_app = create_app(internal_mode=False)
    ext_app.state.redis = fake_redis
    int_app = create_app(internal_mode=True, shared_app=ext_app)
    return TestClient(ext_app), TestClient(int_app), fake_redis


def test_identities_yaml_loads_without_secrets():
    cfg = load_identities_config()
    assert any(op.id == "op-admin" and op.api_key_env == "AEGIS_API_KEY" for op in cfg.operators)
    assert any(wh.id == "wh-alertmanager" and "alerts:ingest" in wh.scopes for wh in cfg.webhooks)
    assert "phase1-worker" in cfg.internal_services


def test_multi_operator_auth_me_and_scoped_access(monkeypatch, mocker):
    ext_client, int_client, fake_redis = _setup_dual_clients(monkeypatch, mocker)

    # Distinct operator API keys resolve distinct usernames via /v1/auth/me
    r_alice = ext_client.get("/v1/auth/me", headers={"X-API-Key": "alice-secret-key"})
    assert r_alice.status_code == 200
    assert r_alice.json()["username"] == "alice"
    assert r_alice.json()["key_id"] == "env:TEST_ALICE_KEY"

    r_bob = ext_client.get("/v1/auth/me", headers={"X-API-Key": "bob-secret-key"})
    assert r_bob.status_code == 200
    assert r_bob.json()["username"] == "bob"
    assert r_bob.json()["role"] == "operator"

    # Bob (role=operator) cannot mutate config (requires config:write)
    r_bob_cfg = ext_client.put("/v1/config/sag/tools/restart_pod", json={"pre_approved": False}, headers={"X-API-Key": "bob-secret-key"})
    assert r_bob_cfg.status_code == 403
    assert "config:write" in r_bob_cfg.json()["detail"]

    # Webhook key is strictly scoped to alerts:ingest
    alert_payload = {
        "groupKey": "gk-1",
        "alerts": [{"labels": {"alertname": "High5xx", "service": "frontend", "namespace": "boutique", "severity": "critical"}}],
    }
    r_wh_ingest = ext_client.post("/v1/alerts/ingest", json=alert_payload, headers={"X-API-Key": "webhook-secret-key"})
    assert r_wh_ingest.status_code == 200
    assert r_wh_ingest.json()["ingested_by"] == "alertmanager-prod"
    assert r_wh_ingest.json()["key_id"] == "env:TEST_WEBHOOK_KEY"

    r_wh_inc = ext_client.get("/v1/incidents", headers={"X-API-Key": "webhook-secret-key"})
    assert r_wh_inc.status_code == 403

    # Read-only external client can read incidents & audit stream, cannot write
    assert ext_client.get("/v1/incidents", headers={"X-API-Key": "client-read-key"}).status_code == 200
    assert ext_client.get("/v1/audit/interactions", headers={"X-API-Key": "client-read-key"}).status_code == 200
    assert ext_client.post("/v1/system/pause", json={"reason": "test"}, headers={"X-API-Key": "client-read-key"}).status_code == 403

    # Dual-port isolation: external port 8080 rejects X-Aegis-Service without API key
    r_spoof = ext_client.get("/v1/incidents", headers={"X-Aegis-Service": "phase1-worker"})
    assert r_spoof.status_code == 403

    # Internal port 8081 accepts registered internal service without API key, rejects unknown service
    r_int_ok = int_client.get("/v1/incidents", headers={"X-Aegis-Service": "phase1-worker"})
    assert r_int_ok.status_code == 200
    r_int_bad = int_client.get("/v1/incidents", headers={"X-Aegis-Service": "rogue-worker"})
    assert r_int_bad.status_code == 403


def test_dashboard_binds_authoritative_username_and_telegram_maps_user_without_handle(monkeypatch, mocker):
    ext_client, int_client, fake_redis = _setup_dual_clients(monkeypatch, mocker)

    # Seed two incidents waiting approval
    for inc_id, act_id in [("inc-dash-1", "act-dash-1"), ("inc-tg-1", "act-tg-1"), ("inc-tg-2", "act-tg-2")]:
        fake_redis.set(
            f"incident:{inc_id}:state",
            json.dumps({
                "incident_id": inc_id,
                "state": "AWAITING_APPROVAL",
                "severity": "P2",
                "affected_service": "frontend",
                "affected_namespace": "boutique",
            }),
        )
        fake_redis.set(
            f"incident:{inc_id}:action:{act_id}",
            json.dumps({
                "action_id": act_id,
                "incident_id": inc_id,
                "status": "pending",
                "proposal": {"confidence": 0.9, "actions": [{"tool_name": "restart_pod", "parameters": {}}]},
            }),
        )

    # 1. Dashboard operator with spoofed/hardcoded actor gets bound to authenticated username "bob"
    resp_dash = ext_client.post(
        "/v1/incidents/inc-dash-1/approve",
        json={"action_id": "act-dash-1", "decision": "approved", "actor": "dashboard-operator", "channel": "dashboard"},
        headers={"X-API-Key": "bob-secret-key"},
    )
    assert resp_dash.status_code == 200
    assert resp_dash.json()["actor"] == "bob"
    saved_dash = json.loads(fake_redis.get("incident:inc-dash-1:action:act-dash-1"))
    assert saved_dash["decided_by"] == "bob"
    assert saved_dash["auth_key_id"] == "env:TEST_BOB_KEY"

    # 2. Telegram user ID 1000000001 with NO @username resolves to mapped Aegis username "alice"
    resp_tg_mapped = int_client.post(
        "/v1/incidents/inc-tg-1/approve",
        json={
            "action_id": "act-tg-1",
            "decision": "approved",
            "actor": "telegram:1000000001:unknown",
            "channel": "telegram",
            "telegram_user_id": "1000000001",
            "telegram_username": None,
            "telegram_full_name": "Alice Display",
        },
        headers={"X-Aegis-Service": "aegis-telegram-bot"},
    )
    assert resp_tg_mapped.status_code == 200
    assert resp_tg_mapped.json()["actor"] == "alice"
    saved_tg1 = json.loads(fake_redis.get("incident:inc-tg-1:action:act-tg-1"))
    assert saved_tg1["decided_by"] == "alice"
    assert saved_tg1["decided_by_meta"]["telegram_user_id"] == "1000000001"
    assert saved_tg1["decided_by_meta"]["mapped"] is True

    # 3. Unmapped Telegram user without @username resolves to full_name (NEVER "unknown")
    resp_tg_unmapped = int_client.post(
        "/v1/incidents/inc-tg-2/approve",
        json={
            "action_id": "act-tg-2",
            "decision": "denied",
            "actor": "John Doe",
            "channel": "telegram",
            "telegram_user_id": "5551234",
            "telegram_username": None,
            "telegram_full_name": "John Doe",
        },
        headers={"X-Aegis-Service": "aegis-telegram-bot"},
    )
    assert resp_tg_unmapped.status_code == 200
    assert resp_tg_unmapped.json()["actor"] == "John Doe"
    saved_tg2 = json.loads(fake_redis.get("incident:inc-tg-2:action:act-tg-2"))
    assert saved_tg2["decided_by"] == "John Doe"
    assert "unknown" not in saved_tg2["decided_by"].lower()


@pytest.mark.asyncio
async def test_webhook_attribution_flows_through_consumer_correlator_and_audit(monkeypatch, mocker):
    ext_client, _, fake_redis = _setup_dual_clients(monkeypatch, mocker)

    payload = {
        "groupKey": "gk-prod-1",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "CheckoutErrorSpike",
                    "service": "checkoutservice",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-audit-1",
            }
        ],
    }
    resp = ext_client.post("/v1/alerts/ingest", json=payload, headers={"X-API-Key": "webhook-secret-key"})
    assert resp.status_code == 200

    from shared.config.schemas import SystemConfig
    await run_alert_consumer(fake_redis, SystemConfig(), topology_store=None, run_once=True)

    inc_id = fake_redis.get("incident:fingerprints:fp-audit-1")
    assert inc_id is not None
    raw_grp = json.loads(fake_redis.get(f"incident:{inc_id}:alert_group"))
    assert raw_grp["ingested_by"] == "alertmanager-prod"
    assert raw_grp["ingested_key_id"] == "env:TEST_WEBHOOK_KEY"
    assert raw_grp["ingested_entity_type"] == "webhook"

    interactions = list_interactions(fake_redis, limit=20)
    assert any(item["actor"] == "alertmanager-prod" and item["category"] == "webhook" for item in interactions)
    assert any(item["category"] == "alert_incident_created" and item["incident_id"] == inc_id for item in interactions)


@pytest.mark.asyncio
async def test_outbound_external_mcp_tool_audit_and_endpoint_auth_headers(mocker):
    fake_redis = AuthAuditFakeRedis()
    set_audit_redis_client(fake_redis)
    token = set_audit_context(incident_id="inc-out-1", caller="phase2-worker")

    mock_session = AsyncMock()
    call_res = MagicMock()
    call_res.model_dump.return_value = {"content": [{"type": "text", "text": "ok"}], "isError": False}
    mock_session.call_tool.return_value = call_res

    class MockStreamable:
        async def __aenter__(self):
            return (MagicMock(), MagicMock(), None)

        async def __aexit__(self, *args):
            pass

    class MockSessionCtx:
        def __init__(self, r, w):
            pass

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    try:
        ext_client = MCPClient(
            server_url="https://ext-mcp.example.com/mcp",
            api_key="super-secret-value",
            server_name="ext_observability",
            external=True,
            api_key_env="EXT_OBS_TOKEN",
        )
        with patch("shared.harness.mcp.streamablehttp_client", return_value=MockStreamable()), \
             patch("shared.harness.mcp.ClientSession", MockSessionCtx):
            res = await ext_client.call_tool("ext_read_metrics", {"query": "up"})
            assert res["isError"] is False
    finally:
        reset_audit_context(token)
        set_audit_redis_client(None)

    audit_items = list_interactions(fake_redis, limit=10, direction="outbound", external_only=True)
    assert len(audit_items) == 1
    assert audit_items[0]["actor"] == "phase2-worker"
    assert audit_items[0]["action"] == "ext_read_metrics"
    assert audit_items[0]["credential_ref"] == "env:EXT_OBS_TOKEN"
    assert "super-secret-value" not in json.dumps(audit_items[0])

    # Ledger entry recorded for external tool call during active incident
    raw_ledger = fake_redis.lrange("incident:inc-out-1:ledger", 0, -1)
    assert len(raw_ledger) >= 1
    entry = LedgerEntry.model_validate_json(raw_ledger[-1])
    assert entry.event_type == "external_tool_call"
    assert entry.details["credential_ref"] == "env:EXT_OBS_TOKEN"
    assert entry.details["external"] is True

    # Outbound auth headers on built-in connectors when API keys configured
    mock_cfg = mocker.patch("shared.config.loader.ConfigManager")
    ep = mock_cfg.return_value.system.mcp_endpoints
    ep.prometheus_url = "https://prom.example.com"
    ep.prometheus_api_key = "prom-key"
    ep.prometheus_auth_type = "bearer"
    ep.prometheus_auth_header = ""
    ep.loki_url = "https://loki.example.com"
    ep.loki_api_key = "loki-key"
    ep.loki_auth_type = "api_key"
    ep.loki_auth_header = "X-Scope-Key"
    ep.tempo_url = "https://tempo.example.com"
    ep.tempo_api_key = "tempo-key"
    ep.tempo_auth_type = "bearer"
    ep.tempo_auth_header = ""
    ep.jaeger_url = "https://jaeger.example.com"
    ep.jaeger_api_key = "jaeger-key"
    ep.jaeger_auth_type = "bearer"
    ep.jaeger_auth_header = ""
    ep.flagd_url = "https://flagd.example.com"
    ep.flagd_api_key = "flagd-key"
    ep.flagd_auth_type = "bearer"
    ep.flagd_auth_header = ""

    mock_http = AsyncMock()
    prom_ok = MagicMock(json=lambda: {"status": "success", "data": {"result": []}})
    prom_ok.raise_for_status = MagicMock()
    mock_http.get.return_value = prom_ok
    await prometheus_query("up", client=mock_http, base_url="https://prom.example.com")
    assert mock_http.get.call_args[1]["headers"] == {"Authorization": "Bearer prom-key"}

    mock_http.reset_mock()
    await loki_query('{app="cart"}', client=mock_http, base_url="https://loki.example.com")
    assert mock_http.get.call_args[1]["headers"] == {"X-Scope-Key": "loki-key"}

    mock_http.reset_mock()
    mock_http.get.return_value = MagicMock(json=lambda: {"traces": []}, raise_for_status=MagicMock())
    await tempo_search("cart", client=mock_http, base_url="https://tempo.example.com")
    assert mock_http.get.call_args[1]["headers"] == {"Authorization": "Bearer tempo-key"}

    mock_http.reset_mock()
    mock_http.get.return_value = MagicMock(json=lambda: {"data": []}, raise_for_status=MagicMock())
    await jaeger_search_traces("cart", client=mock_http, base_url="https://jaeger.example.com")
    assert mock_http.get.call_args[1]["headers"] == {"Authorization": "Bearer jaeger-key"}

    mock_http.reset_mock()
    mock_http.get.return_value = MagicMock(json=lambda: {"flags": {}}, raise_for_status=MagicMock())
    await get_feature_flag(flagd_url="https://flagd.example.com", client=mock_http)
    assert mock_http.get.call_args[1]["headers"] == {"Authorization": "Bearer flagd-key"}


def test_config_history_capped_at_8_per_file_and_prunes_disk_files(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    agents_file = cfg_dir / "agents.yaml"
    sag_file = cfg_dir / "sag_policies.yaml"

    monkeypatch.setenv("AEGIS_CONFIG_DIR", str(cfg_dir))

    # Create 11 snapshots of agents.yaml and 2 snapshots of sag_policies.yaml
    for i in range(11):
        agents_file.write_text(f"version: {i}\n", encoding="utf-8")
        vid = config_routes._save_version_snapshot("agents.yaml", f"update {i}", actor="alice")
        assert vid

    for i in range(2):
        sag_file.write_text(f"sag: {i}\n", encoding="utf-8")
        vid = config_routes._save_version_snapshot("sag_policies.yaml", f"sag {i}", actor="bob")
        assert vid

    h_dir = cfg_dir / "history"
    agents_hist = config_routes._list_history("agents.yaml")
    sag_hist = config_routes._list_history("sag_policies.yaml")

    assert len(agents_hist) == 8
    assert len(sag_hist) == 2
    assert agents_hist[0]["description"] == "update 10"
    assert agents_hist[-1]["description"] == "update 3"
    assert agents_hist[0]["actor"] == "alice"

    agents_files = sorted(p.name for p in h_dir.glob("agents_*.yaml"))
    sag_files = sorted(p.name for p in h_dir.glob("sag_policies_*.yaml"))
    assert len(agents_files) == 8
    assert len(sag_files) == 2


def test_telegram_user_id_env_resolution_and_redaction(monkeypatch):
    cfg = IdentitiesConfig(
        operators=[
            OperatorIdentityConfig(
                id="op-john",
                username="john.doe",
                display_name="John Doe",
                role="operator",
                api_key_env="TEST_JOHN_KEY",
                telegram_user_id_env="TEST_JOHN_TG_ID",
            )
        ]
    )
    monkeypatch.setenv("TEST_JOHN_KEY", "john-secret-key")
    monkeypatch.delenv("TEST_JOHN_TG_ID", raising=False)

    reg_unset = IdentityRegistry.from_config(cfg)
    pub_unset = reg_unset.list_public_identities()["operators"][0]
    assert pub_unset["telegram_user_id_env"] == "TEST_JOHN_TG_ID"
    assert pub_unset["telegram_configured"] is False
    assert pub_unset["telegram_user_id"] is None
    actor_unset, meta_unset = reg_unset.resolve_telegram_actor("777888999", telegram_full_name="Unknown Caller")
    assert actor_unset == "Unknown Caller"
    assert meta_unset["mapped"] is False

    monkeypatch.setenv("TEST_JOHN_TG_ID", "777888999")
    reg_set = IdentityRegistry.from_config(cfg)
    pub_set = reg_set.list_public_identities()["operators"][0]
    assert pub_set["telegram_user_id_env"] == "TEST_JOHN_TG_ID"
    assert pub_set["telegram_configured"] is True
    assert pub_set["telegram_user_id"] is None

    principal = reg_set.authenticate_api_key("john-secret-key")
    assert principal is not None
    assert principal.telegram_user_id == "777888999"

    actor_set, meta_set = reg_set.resolve_telegram_actor("777888999", telegram_full_name="John Doe")
    assert actor_set == "john.doe"
    assert meta_set["mapped"] is True
    assert meta_set["operator_id"] == "op-john"


