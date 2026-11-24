"""Unit tests for out-of-scope namespace drop-gating, SHA-256 drift versioning, and hybrid runbook catalog."""

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr
import pytest
import redis

from services.api_server.ingestion.correlator import AlertCorrelator
from services.api_server.routers.drift import router as drift_router
from services.api_server.routers.incidents import router as incidents_router
from services.api_server.topology.store import TopologyStore
from services.api_server.topology.sync import detect_topology_drift
from services.phase2_worker.graph import create_remediation_graph, Phase2GraphState
from services.phase2_worker.main import Phase2WorkerService
from services.phase2_worker.runbook_index import (
    search_runbook_catalog,
    should_use_two_step_catalog,
)
from shared.audit.tracker import (
    AUDIT_STREAM_KEY,
    DROPPED_ALERTS_STREAM_KEY,
    list_dropped_alerts,
    list_interactions,
)
from shared.config.loader import ConfigManager
from shared.config.schemas import IngestionConfig
from shared.models.actions import ActionPlanItem, ActionProposal
from shared.models.alerts import Alert
from shared.models.topology import TopologyEdge, TopologyNode
from shared.versioning import (
    RUNBOOKS_META_KEY,
    TOPOLOGY_META_KEY,
    compute_runbooks_digest,
    compute_topology_digest,
    get_pending_drift,
    read_meta,
    write_meta,
)
from scripts.seed_runbooks import detect_runbook_drift, sync_runbook_index_and_qdrant


class ComprehensiveFakeRedis(redis.Redis):
    """In-memory Redis fake supporting KV, lists, sets, and Redis Streams."""

    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}
        self.lists: Dict[str, List[Any]] = {}
        self.sets: Dict[str, set] = {}
        self.streams: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
        self.expirations: Dict[str, int] = {}
        self._seq = 0

    def set(self, key: str, value: Any, *args: Any, **kwargs: Any) -> bool:
        self.store[key] = value
        return True

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
            self.expirations.pop(key, None)
        return removed

    def rpush(self, key: str, value: Any) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lrange(self, key: str, start: int, end: int) -> List[Any]:
        items = self.lists.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    def incr(self, key: str) -> int:
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = val
        return val

    def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True

    def sadd(self, key: str, member: Any) -> int:
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.add(member)
        return 1 if len(s) > before else 0

    def smembers(self, key: str) -> set:
        return set(self.sets.get(key, set()))

    def srem(self, key: str, member: Any) -> int:
        s = self.sets.get(key)
        if s and member in s:
            s.remove(member)
            return 1
        return 0

    def keys(self, pattern: str = "*") -> List[str]:
        import fnmatch
        all_keys = set(self.store.keys()) | set(self.lists.keys()) | set(self.sets.keys())
        return [k for k in all_keys if fnmatch.fnmatch(k, pattern)]

    def scan_iter(self, match: str = "*"):
        for k in self.keys(match):
            yield k

    def xadd(self, stream: str, fields: Dict[str, Any], maxlen: Optional[int] = None, approximate: bool = False) -> str:
        self._seq += 1
        msg_id = f"1700000000-{self._seq}"
        self.streams.setdefault(stream, []).append((msg_id, dict(fields)))
        return msg_id

    def xrevrange(self, stream: str, max: str = "+", min: str = "-", count: int = 100) -> List[tuple[str, Dict[str, Any]]]:
        items = list(reversed(self.streams.get(stream, [])))
        return items[:count]

    def xrange(self, stream: str, min: str = "-", max: str = "+", count: int = 100) -> List[tuple[str, Dict[str, Any]]]:
        items = list(self.streams.get(stream, []))
        return items[:count]

    def xack(self, stream: str, group: str, msg_id: str) -> int:
        return 1


class SequenceToolFakeLLM(BaseChatModel):
    """Scripted chat model emitting tool calls before returning the final response."""

    responses: List[AIMessage]
    _calls: List[List[BaseMessage]] = PrivateAttr(default_factory=list)
    _cursor: int = PrivateAttr(default=0)

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls.append(list(messages))
        idx = min(self._cursor, len(self.responses) - 1)
        self._cursor += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])

    @property
    def _llm_type(self) -> str:
        return "sequence-tool-fake"


def _make_config(
    monitored_namespaces: Optional[List[str]] = None,
    allowed_namespaces: Optional[List[str]] = None,
    catalog_mode: str = "auto",
    catalog_inline_max_entries: int = 15,
) -> SimpleNamespace:
    sys_cfg = SimpleNamespace(
        system=SimpleNamespace(api_url="http://localhost:8080", api_key="test-key"),
        redis=SimpleNamespace(url="redis://localhost:6379/0"),
        ingestion=IngestionConfig(allowed_namespaces=allowed_namespaces or []),
        topology=SimpleNamespace(monitored_namespaces=monitored_namespaces or ["boutique"], helm_dir="./charts"),
        qdrant=SimpleNamespace(
            url="http://localhost:6333",
            collection="aegis_runbooks",
            embedding_provider="ollama",
            embedding_model="nomic-embed-text",
            catalog_mode=catalog_mode,
            catalog_inline_max_entries=catalog_inline_max_entries,
        ),
        llm=SimpleNamespace(ollama_url="http://localhost:11434"),
        notifications=SimpleNamespace(telegram_enabled=False, telegram_bot_token="", telegram_chat_id=""),
    )
    agents_cfg = SimpleNamespace(
        agents={
            "remediation": SimpleNamespace(max_tool_calls=8, max_iter=8),
        }
    )
    sag_cfg = SimpleNamespace(max_retries={"P1": 3, "P2": 3, "P3": 3})
    return SimpleNamespace(system=sys_cfg, agents=agents_cfg, sag=sag_cfg)


def _sample_catalog_entries(count: int, include_rule: bool = True) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if include_rule:
        entries.append({
            "id": "rule-never-drop-db",
            "title": "Database Preservation Rule",
            "types": ["rule"],
            "symptoms": ["database", "pvc"],
            "tags": ["safety"],
            "summary": "Never delete stateful database PVCs.",
            "allowed_actions": ["restart_pod"],
            "sections": [{"section_name": "Mandate", "line_range": "L1-L5", "chunk_id": "chunk 1/1", "excerpt": "Preserve PVC state."}],
        })
    start_idx = 1 if include_rule else 0
    for i in range(start_idx, count):
        is_target = i == count - 1
        entries.append({
            "id": f"rb-{i:02d}" + ("-cartservice-oom" if is_target else "-generic"),
            "title": "Cartservice OOM Runbook" if is_target else f"Generic Runbook {i}",
            "types": ["runbook"],
            "symptoms": ["OOMKilled", "exit code 137", "cartservice"] if is_target else ["latency"],
            "tags": ["cartservice", "memory"] if is_target else [f"svc-{i}"],
            "summary": "Increase cartservice memory limits when OOMKilled" if is_target else f"Summary {i}",
            "allowed_actions": ["update_resource_limits", "restart_pod"] if is_target else ["restart_pod"],
            "sections": [
                {
                    "section_name": "Remediation Steps",
                    "line_range": "L10-L20",
                    "chunk_id": "chunk 1/2",
                    "excerpt": "Verify exit code 137 then expand memory limits.",
                }
            ],
        })
    return entries


# ---------------------------------------------------------------------------
# 1. Out-of-scope namespace drop-gate & Redis drop logging
# ---------------------------------------------------------------------------

def test_ingest_drops_out_of_scope_namespace_and_logs_to_redis():
    fake_redis = ComprehensiveFakeRedis()
    app = FastAPI()
    app.include_router(incidents_router, prefix="/v1")
    app.state.redis = fake_redis
    app.state.config = _make_config(monitored_namespaces=["boutique", "otel-demo"])

    client = TestClient(app)
    resp = client.post(
        "/v1/alerts/ingest",
        json={
            "alerts": [
                {
                    "fingerprint": "fp-kube-1",
                    "labels": {
                        "alertname": "KubePodCrashLooping",
                        "service": "coredns",
                        "namespace": "kube-system",
                        "severity": "P2",
                    },
                    "annotations": {"summary": "CoreDNS crashing"},
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dropped_out_of_scope"
    assert body["dropped_count"] == 1
    assert body["dropped_namespaces"] == ["kube-system"]
    assert fake_redis.streams.get("stream:alert:raw", []) == []

    dropped = list_dropped_alerts(fake_redis)
    assert len(dropped) == 1
    assert dropped[0]["namespace"] == "kube-system"
    assert dropped[0]["alertname"] == "KubePodCrashLooping"
    assert dropped[0]["reason"] == "namespace_not_monitored"

    audits = list_interactions(fake_redis)
    assert len(audits) == 1
    assert audits[0]["category"] == "alert_dropped_namespace"

    dropped_api = client.get("/v1/alerts/dropped", params={"namespace": "kube-system"})
    assert dropped_api.status_code == 200
    assert dropped_api.json()["count"] == 1
    assert dropped_api.json()["items"][0]["service"] == "coredns"


def test_ingest_mixed_batch_filters_out_of_scope_and_keeps_monitored():
    fake_redis = ComprehensiveFakeRedis()
    app = FastAPI()
    app.include_router(incidents_router, prefix="/v1")
    app.state.redis = fake_redis
    app.state.config = _make_config(monitored_namespaces=["boutique"])

    client = TestClient(app)
    resp = client.post(
        "/v1/alerts/ingest",
        json={
            "alerts": [
                {
                    "fingerprint": "fp-in-1",
                    "labels": {
                        "alertname": "HighErrorRate",
                        "service": "checkoutservice",
                        "namespace": "boutique",
                        "severity": "P1",
                    },
                    "annotations": {"summary": "5xx spike"},
                },
                {
                    "fingerprint": "fp-out-1",
                    "labels": {
                        "alertname": "ExternalAlert",
                        "service": "cert-manager",
                        "namespace": "cert-manager",
                        "severity": "P3",
                    },
                    "annotations": {"summary": "Renew failure"},
                },
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["dropped_count"] == 1

    queued_msgs = fake_redis.streams.get("stream:alert:raw", [])
    assert len(queued_msgs) == 1
    enqueued_payload = json.loads(queued_msgs[0][1]["payload"])
    assert [a["labels"]["namespace"] for a in enqueued_payload["alerts"]] == ["boutique"]
    assert len(fake_redis.streams.get(DROPPED_ALERTS_STREAM_KEY, [])) == 1
    assert len(fake_redis.streams.get(AUDIT_STREAM_KEY, [])) == 1


def test_ingest_prefers_ingestion_allowed_namespaces_override():
    fake_redis = ComprehensiveFakeRedis()
    app = FastAPI()
    app.include_router(incidents_router, prefix="/v1")
    app.state.redis = fake_redis
    app.state.config = _make_config(
        monitored_namespaces=["boutique"],
        allowed_namespaces=["payments"],
    )

    client = TestClient(app)
    r_drop = client.post(
        "/v1/alerts/ingest",
        json={
            "alerts": [
                {
                    "labels": {
                        "alertname": "CartDown",
                        "service": "cartservice",
                        "namespace": "boutique",
                        "severity": "P2",
                    },
                    "annotations": {"summary": "Boutique blocked by explicit ingestion override"},
                }
            ]
        },
    )
    assert r_drop.status_code == 200
    assert r_drop.json()["status"] == "dropped_out_of_scope"

    r_keep = client.post(
        "/v1/alerts/ingest",
        json={
            "alerts": [
                {
                    "labels": {
                        "alertname": "LedgerLag",
                        "service": "ledger",
                        "namespace": "payments",
                        "severity": "P2",
                    },
                    "annotations": {"summary": "Payments in override"},
                }
            ]
        },
    )
    assert r_keep.status_code == 200
    assert r_keep.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_correlator_defense_in_depth_drops_out_of_scope(tmp_path):
    fake_redis = ComprehensiveFakeRedis()
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "snap.json"))
    correlator = AlertCorrelator(
        fake_redis,
        IngestionConfig(allowed_namespaces=["boutique"]),
        topology_store=store,
        allowed_namespaces=["boutique"],
    )
    out_payload = {
        "alerts": [
            {
                "fingerprint": "fp-corr-out",
                "labels": {
                    "alertname": "NodePressure",
                    "service": "kubelet",
                    "namespace": "kube-system",
                    "severity": "P2",
                },
                "annotations": {"summary": "Outside namespace"},
            }
        ]
    }
    result = await correlator.process(out_payload, ingest_meta={"ingested_by": "alertmanager"})
    assert result is None
    assert fake_redis.streams.get("stream:phase1:jobs", []) == []
    dropped = list_dropped_alerts(fake_redis)
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "namespace_not_monitored"
    assert dropped[0]["ingested_by"] == "alertmanager"


# ---------------------------------------------------------------------------
# 2. Topology SHA-256 versioning & operator-gated drift
# ---------------------------------------------------------------------------

def test_topology_store_sha_skip_and_force_bump(tmp_path):
    fake_redis = ComprehensiveFakeRedis()
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "topo.json"))
    n1 = [TopologyNode(id="frontend.boutique", name="frontend", namespace="boutique", ports=[8080], dependencies=[])]
    e1 = [TopologyEdge(**{"from": "frontend.boutique", "to": "cartservice.boutique", "port": 7070})]

    res1 = store.save(n1, e1, updated_by="boot")
    assert res1["changed"] is True
    assert res1["version"] == 1
    sha1 = res1["sha256"]
    assert len(sha1) == 64

    res2 = store.save(n1, e1, updated_by="periodic")
    assert res2["changed"] is False
    assert res2["version"] == 1
    assert res2["sha256"] == sha1

    res3 = store.save(n1, e1, updated_by="admin", force=True)
    assert res3["changed"] is True
    assert res3["version"] == 2
    assert res3["sha256"] == sha1

    n2 = [
        *n1,
        TopologyNode(id="paymentservice.boutique", name="paymentservice", namespace="boutique", ports=[50051], dependencies=[]),
    ]
    res4 = store.save(n2, e1, updated_by="admin")
    assert res4["changed"] is True
    assert res4["version"] == 3
    assert res4["sha256"] != sha1
    assert read_meta(fake_redis, TOPOLOGY_META_KEY)["version"] == 3


def test_topology_drift_stage_reject_approve_and_sync_endpoints(tmp_path):
    fake_redis = ComprehensiveFakeRedis()
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "topo.json"))
    initial_nodes = [TopologyNode(id="frontend.boutique", name="frontend", namespace="boutique", ports=[8080], dependencies=[])]
    drifted_nodes = [
        *initial_nodes,
        TopologyNode(id="paymentservice.boutique", name="paymentservice", namespace="boutique", ports=[50051], dependencies=[]),
    ]
    store.save(initial_nodes, [], updated_by="boot")

    app = FastAPI()
    app.include_router(drift_router, prefix="/v1")
    app.state.redis = fake_redis
    app.state.topology_store = store
    app.state.config = _make_config(monitored_namespaces=["boutique"])
    client = TestClient(app)

    with patch(
        "services.api_server.topology.sync.build_live_topology",
        new=AsyncMock(return_value=(drifted_nodes, [])),
    ):
        chk = client.post("/v1/drift/check?target=topology")
        assert chk.status_code == 200
        assert chk.json()["results"]["topology"]["status"] == "pending"
        assert "paymentservice.boutique" in chk.json()["results"]["topology"]["summary"]["added_nodes"]
        # Store must NOT be overwritten yet
        assert [n.id for n in store.all_nodes()] == ["frontend.boutique"]
        assert get_pending_drift(fake_redis, "topology") is not None

        status_resp = client.get("/v1/drift/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["pending"]["topology"]["new_sha"] == compute_topology_digest(drifted_nodes, [])

        # Reject clears pending without altering active topology
        rej = client.post("/v1/drift/topology/reject", json={"reason": "Maintenance window", "actor": "alice"})
        assert rej.status_code == 200
        assert rej.json()["status"] == "rejected"
        assert get_pending_drift(fake_redis, "topology") is None
        assert [n.id for n in store.all_nodes()] == ["frontend.boutique"]

        # Re-detect then approve applies v2
        client.post("/v1/drift/check?target=topology")
        app_resp = client.post("/v1/drift/topology/approve", json={"actor": "alice"})
        assert app_resp.status_code == 200
        assert app_resp.json()["status"] == "approved"
        assert app_resp.json()["version"] == 2
        assert sorted(n.id for n in store.all_nodes()) == ["frontend.boutique", "paymentservice.boutique"]
        assert get_pending_drift(fake_redis, "topology") is None

        # Manual sync skips when unchanged; force sync bumps to v3
        sync_noop = client.post("/v1/drift/topology/sync")
        assert sync_noop.status_code == 200
        assert sync_noop.json()["changed"] is False
        assert sync_noop.json()["version"] == 2

        sync_force = client.post("/v1/drift/topology/sync?force=true")
        assert sync_force.status_code == 200
        assert sync_force.json()["changed"] is True
        assert sync_force.json()["version"] == 3


# ---------------------------------------------------------------------------
# 3. Runbooks SHA-256 versioning, drift endpoints & Phase 2 hot-reload
# ---------------------------------------------------------------------------

def _write_sample_runbook(directory: Path, name: str, rb_id: str, title: str, symptom: str) -> None:
    content = (
        "---\n"
        f"id: {rb_id}\n"
        f"title: {title}\n"
        "version: 1.0\n"
        "types: [runbook]\n"
        f"symptoms: [{symptom}]\n"
        "tags: [boutique]\n"
        "allowed_actions: [restart_pod]\n"
        "---\n\n"
        f"## Remediation Steps\nRestart affected service for {symptom}.\n"
    )
    (directory / name).write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_runbooks_drift_stage_reject_approve_and_force_sync(tmp_path):
    fake_redis = ComprehensiveFakeRedis()
    rb_dir = tmp_path / "runbooks"
    rb_dir.mkdir()
    index_file = tmp_path / "runbooks_index.json"
    _write_sample_runbook(rb_dir, "rb1.md", "rb-one", "Runbook One", "timeout")

    cfg = _make_config(monitored_namespaces=["boutique"])
    with patch("scripts.seed_runbooks.seed_qdrant_runbooks", new=AsyncMock(return_value=1)):
        init_res = await sync_runbook_index_and_qdrant(
            cfg.system,
            redis_client=fake_redis,
            runbooks_dir=rb_dir,
            index_output_path=index_file,
        )
    assert init_res["changed"] is True
    assert init_res["version"] == 1
    assert (tmp_path / ".runbooks_index_meta.json").is_file()

    # Add new runbook markdown -> drift detected, disk index NOT overwritten yet
    _write_sample_runbook(rb_dir, "rb2.md", "rb-two", "Runbook Two", "oom")
    app = FastAPI()
    app.include_router(drift_router, prefix="/v1")
    app.state.redis = fake_redis
    app.state.config = cfg
    client = TestClient(app)

    def _mock_paths(*_args, **_kwargs):
        return rb_dir, index_file

    with (
        patch("scripts.seed_runbooks._resolve_runbooks_paths", side_effect=_mock_paths),
        patch("services.api_server.routers.drift._resolve_runbooks_paths", side_effect=_mock_paths),
        patch("scripts.seed_runbooks.seed_qdrant_runbooks", new=AsyncMock(return_value=2)),
    ):
        chk = client.post("/v1/drift/check?target=runbooks")
        assert chk.status_code == 200
        rb_drift = chk.json()["results"]["runbooks"]
        assert rb_drift["status"] == "pending"
        assert rb_drift["summary"]["added_runbooks"] == ["rb-two"]
        assert get_pending_drift(fake_redis, "runbooks") is not None

        rej = client.post("/v1/drift/runbooks/reject", json={"reason": "Hold runbook push"})
        assert rej.status_code == 200
        assert rej.json()["status"] == "rejected"
        assert get_pending_drift(fake_redis, "runbooks") is None

        client.post("/v1/drift/check?target=runbooks")
        app_resp = client.post("/v1/drift/runbooks/approve", json={"actor": "sre-lead"})
        assert app_resp.status_code == 200
        assert app_resp.json()["status"] == "approved"
        assert app_resp.json()["version"] == 2
        assert read_meta(fake_redis, RUNBOOKS_META_KEY)["version"] == 2

        sync_force = client.post("/v1/drift/runbooks/sync?force=true")
        assert sync_force.status_code == 200
        assert sync_force.json()["version"] == 3
        assert sync_force.json()["status"] == "rebuilt"


@pytest.mark.asyncio
async def test_phase2_worker_hot_reloads_runbook_index_on_meta_sha_change():
    fake_redis = ComprehensiveFakeRedis()
    write_meta(fake_redis, RUNBOOKS_META_KEY, {"sha256": "sha-v1", "version": 1})

    fake_redis.set(
        "incident:inc-hot-1",
        json.dumps({"incident_id": "inc-hot-1", "severity": "P2", "state": "REMEDIATION", "affected_service": "cartservice"}),
    )
    fake_redis.set("incident:inc-hot-1:context", json.dumps({"primary_hypothesis": "OOM"}))

    captured_states: List[Dict[str, Any]] = []

    class CapturingGraph:
        checkpointer = None

        async def ainvoke(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            captured_states.append(dict(state))
            return {"outcome": "low_confidence"}

    v1_entries = _sample_catalog_entries(2)
    v2_entries = _sample_catalog_entries(3)

    with (
        patch("services.phase2_worker.main.load_runbooks_index", return_value=v1_entries),
        patch("services.phase2_worker.main.get_config", return_value=_make_config()),
    ):
        worker = Phase2WorkerService(redis_client=fake_redis, graph=CapturingGraph())
    worker._post_transition = AsyncMock(return_value=True)
    assert worker._runbook_index_sha == "sha-v1"

    # Simulate approved runbook drift bumping RUNBOOKS_META_KEY in Redis
    write_meta(fake_redis, RUNBOOKS_META_KEY, {"sha256": "sha-v2", "version": 2})
    with patch("services.phase2_worker.main.load_runbooks_index", return_value=v2_entries) as mock_reload:
        await worker.process_job("1-0", {"incident_id": "inc-hot-1"})

    assert mock_reload.called
    assert worker._runbook_index_sha == "sha-v2"
    assert len(captured_states) == 1
    assert captured_states[0]["runbook_index"] == v2_entries


# ---------------------------------------------------------------------------
# 4. Hybrid Auto Runbook Catalog Mode (Direct Inline vs 2-Step Retrieval)
# ---------------------------------------------------------------------------

def test_should_use_two_step_catalog_and_search_ranking():
    small_index = _sample_catalog_entries(5)
    large_index = _sample_catalog_entries(18)

    assert should_use_two_step_catalog(small_index, mode="auto", max_inline=15) is False
    assert should_use_two_step_catalog(large_index, mode="auto", max_inline=15) is True
    assert should_use_two_step_catalog(large_index, mode="direct", max_inline=15) is False
    assert should_use_two_step_catalog(small_index, mode="two_step", max_inline=15) is True

    res = search_runbook_catalog(
        large_index,
        query="OOMKilled memory limits",
        symptoms="exit code 137",
        service="cartservice",
        limit=3,
    )
    assert res["mode"] == "two_step_catalog"
    assert res["total_indexed"] == len(large_index)
    assert res["match_count"] >= 1
    assert res["matches"][0]["id"] == "rb-17-cartservice-oom"
    assert res["matches"][0]["sections"][0]["line_range"] == "L10-L20"


@pytest.mark.asyncio
async def test_graph_hybrid_auto_mode_switches_to_two_step_above_threshold():
    final_proposal = ActionProposal(
        incident_id="inc-cat-1",
        rationale="Increase cartservice memory per runbook rb-17-cartservice-oom (L10-L20).",
        actions=[
            ActionPlanItem(
                tool_name="update_resource_limits",
                target_resource="boutique/cartservice",
                parameters={"namespace": "boutique", "deployment": "cartservice", "limits": {"cpu": "500m", "memory": "1Gi"}},
                rationale="Expand memory limit after OOMKilled.",
            )
        ],
        confidence=0.92,
        risk_assessment="Low risk",
    ).model_dump_json()

    real_cfg = ConfigManager()
    real_cfg.system.qdrant.catalog_mode = "auto"
    real_cfg.system.qdrant.catalog_inline_max_entries = 15

    # Case A: Auto mode with <= 15 entries -> inline <runbook_catalog>, <operational_rules> preserved
    small_llm = SequenceToolFakeLLM(responses=[AIMessage(content=final_proposal)])
    with patch("services.phase2_worker.graph.ConfigManager", return_value=real_cfg):
        graph_small = create_remediation_graph(llm=small_llm, qdrant_fn=AsyncMock(return_value={"results": []}))
        res_small = await graph_small.ainvoke(
            {
                "incident_id": "inc-cat-1",
                "severity": "P2",
                "affected_service": "cartservice",
                "affected_namespace": "boutique",
                "merged_finding": {"primary_hypothesis": "cartservice OOM"},
                "runbook_index": _sample_catalog_entries(4),
                "retry_count": 0,
            }
        )
    assert res_small["outcome"] == "action_proposed"
    first_sys = small_llm._calls[0][0].content
    first_human = small_llm._calls[0][1].content
    assert "<operational_rules>" in first_sys
    assert "Never delete stateful database PVCs." in first_sys
    assert "<runbook_catalog>" in first_human
    assert "<runbook_catalog_mode>" not in first_human

    # Case B: Auto mode with > 15 entries -> two-step retrieval (search_runbook_catalog -> qdrant_search -> ActionProposal)
    two_step_llm = SequenceToolFakeLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc-cat-1",
                        "name": "search_runbook_catalog",
                        "args": {"query": "OOMKilled", "service": "cartservice", "limit": 3},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc-qdrant-1",
                        "name": "qdrant_search",
                        "args": {"id": "rb-17-cartservice-oom"},
                    }
                ],
            ),
            AIMessage(content=final_proposal),
        ]
    )
    mock_qdrant = AsyncMock(return_value={"results": [{"runbook_id": "rb-17-cartservice-oom", "section": "Remediation Steps", "line_range": "L10-L20", "chunk_id": "chunk 1/2", "content": "Raise memory limits."}]})
    with patch("services.phase2_worker.graph.ConfigManager", return_value=real_cfg):
        graph_large = create_remediation_graph(llm=two_step_llm, qdrant_fn=mock_qdrant)
        res_large = await graph_large.ainvoke(
            {
                "incident_id": "inc-cat-1",
                "severity": "P2",
                "affected_service": "cartservice",
                "affected_namespace": "boutique",
                "merged_finding": {"primary_hypothesis": "cartservice OOM"},
                "runbook_index": _sample_catalog_entries(18),
                "retry_count": 0,
            }
        )
    assert res_large["outcome"] == "action_proposed"
    assert len(two_step_llm._calls) == 3

    large_sys = two_step_llm._calls[0][0].content
    large_human = two_step_llm._calls[0][1].content
    assert "<operational_rules>" in large_sys
    assert "Never delete stateful database PVCs." in large_sys
    assert "<runbook_catalog_mode>" in large_human
    assert "<runbook_catalog>" not in large_human

    # Step 2 received the search_runbook_catalog ToolMessage with ranked matches & budget status
    tool_msg_step1 = two_step_llm._calls[1][-1]
    assert tool_msg_step1.name == "search_runbook_catalog"
    parsed_step1 = json.loads(tool_msg_step1.content)
    assert parsed_step1["matches"][0]["id"] == "rb-17-cartservice-oom"
    assert "Step 1 of" in parsed_step1["_budget_status"]

    mock_qdrant.assert_awaited_once()

