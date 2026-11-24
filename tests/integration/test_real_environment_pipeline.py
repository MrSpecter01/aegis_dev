"""Real-environment integration tests against live Docker Aegis services.

Exercises:
- Static Helm chart parsing -> real Redis TopologyStore -> 2-hop BFS adjacency
- Real Prometheus/Tempo HTTP endpoints for discover_runtime_trace_topology
- Real Redis AlertCorrelator: missed open-incident recovery, 2-hop cascade correlation,
  and chronic recurrence window pruning
- Real Qdrant + Ollama semantic retrieval on aegis_runbooks_otel_demo
- Full-order pipeline: Phase 1 AgentFindings (including zero-tool status) ->
  merge_findings -> MergedFinding.model_dump -> Phase 2 create_remediation_graph
  with real Qdrant search and real Prometheus convergence evaluation.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List
import httpx
import pytest
import redis

from services.api_server.ingestion.correlator import AlertCorrelator
from services.api_server.topology.discovery import discover_runtime_trace_topology
from services.api_server.topology.parser import parse_helm_dir
from services.api_server.topology.query import get_adjacent_services
from services.api_server.topology.store import TopologyStore
from services.mcp.tools.qdrant import qdrant_search
from services.phase1_worker.merge import merge_findings
from services.phase2_worker.convergence import run_convergence_monitor
from services.phase2_worker.graph import create_remediation_graph
from shared.config.schemas import (
    ConvergenceConfig,
    ConvergenceSLOConfig,
    IngestionConfig,
    SystemConfig,
)
from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding, EvidenceItem
from shared.models.actions import ActionPlanItem, ActionProposal
from tests.helpers.fake_llm import FakeLLM

def _host_url(raw_url: str, default_url: str) -> str:
    """Normalize Docker-internal DNS names loaded from deploy/.env for host integration execution."""
    from urllib.parse import urlparse, urlunparse

    url = (raw_url or default_url).strip()
    parsed = urlparse(url)
    if parsed.hostname in ("host.docker.internal", "qdrant", "prometheus", "tempo", "aegis-api", "redis"):
        netloc = f"localhost:{parsed.port}" if parsed.port else "localhost"
        return urlunparse(parsed._replace(netloc=netloc))
    return url


REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
if REDIS_HOST == "redis":
    REDIS_HOST = "localhost"
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QDRANT_URL = _host_url(os.getenv("QDRANT_URL", ""), "http://localhost:6333")
OLLAMA_URL = _host_url(os.getenv("OLLAMA_URL", ""), "http://localhost:11434")
PROMETHEUS_URL = _host_url(os.getenv("PROMETHEUS_URL", ""), "http://localhost:9090")
TEMPO_URL = _host_url(os.getenv("TEMPO_URL", ""), "http://localhost:3200")
API_URL = _host_url(os.getenv("AEGIS_API_URL", ""), "http://localhost:8080")


def _live_redis() -> redis.Redis:
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        if not client.ping():
            pytest.skip("Live Redis not responding")
    except Exception as exc:
        pytest.skip(f"Live Redis unavailable at {REDIS_HOST}:{REDIS_PORT}: {exc}")
    return client


def _require_http(url: str, name: str) -> None:
    try:
        resp = httpx.get(url, timeout=3.0)
        if resp.status_code >= 500:
            pytest.skip(f"{name} unhealthy ({resp.status_code})")
    except Exception as exc:
        pytest.skip(f"{name} unavailable at {url}: {exc}")


@pytest.mark.asyncio
async def test_real_redis_static_chart_topology_and_two_hop_correlator(tmp_path):
    """Real Boutique chart + live Redis: 2-hop topology, open recovery past window, chronic pruning."""
    r = _live_redis()
    test_ns = "testrealpipe"
    chart_dir = Path(__file__).resolve().parents[2] / "testbeds" / "boutique" / "chart" / "templates"
    nodes, edges = parse_helm_dir(str(chart_dir), namespace=test_ns)
    assert len(nodes) >= 10
    assert len(edges) >= 5

    store = TopologyStore(r, snapshot_path=str(tmp_path / "topo_snap.json"))
    created_incidents: List[str] = []

    try:
        store.save(nodes, edges)

        adj_1 = get_adjacent_services(store, f"frontend.{test_ns}", max_hops=1)
        adj_2 = get_adjacent_services(store, f"frontend.{test_ns}", max_hops=2)
        assert f"cartservice.{test_ns}" in adj_1
        assert f"redis-cart.{test_ns}" not in adj_1
        assert f"redis-cart.{test_ns}" in adj_2

        cfg = IngestionConfig(
            correlation_window_seconds=180,
            recurrence_window_seconds=900,
            recurrence_threshold=3,
            topology_max_hops=2,
            flap_window_seconds=60,
            flap_threshold=20,
        )
        correlator = AlertCorrelator(r, cfg, store)

        # 1. Seed 1 stale (>900s) and 2 fresh (<900s) recurrence entries for redis-cart
        rec_key = f"recurrence:history:{test_ns}:redis-cart:RealTestRedisDown"
        now = time.time()
        r.set(
            rec_key,
            json.dumps(
                [
                    {"incident_id": "inc-stale-old", "timestamp": now - 1800, "outcome": "ESCALATED"},
                    {"incident_id": "inc-fresh-1", "timestamp": now - 240, "outcome": "ESCALATED"},
                    {"incident_id": "inc-fresh-2", "timestamp": now - 90, "outcome": "ESCALATED"},
                ]
            ),
        )

        inc1 = await correlator.process(
            {
                "groupKey": "real-pipe-redis",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "RealTestRedisDown",
                            "service": "redis-cart",
                            "namespace": test_ns,
                            "severity": "critical",
                        },
                        "fingerprint": "fp-realpipe-redis-1",
                    }
                ],
            }
        )
        assert inc1 is not None
        created_incidents.append(inc1)

        grp1 = correlator.get_alert_group(inc1)
        assert grp1 is not None
        assert grp1.chronic_recurrence is True
        assert grp1.recurrence_count == 3
        assert all(rec["incident_id"] != "inc-stale-old" for rec in grp1.recent_recurrences)

        # 2. Drop from active set while inside 180s window and in REMEDIATION (OPEN), verifying recovery
        r.srem(f"active:namespace:{test_ns}", inc1)
        grp_data = json.loads(r.get(f"incident:{inc1}:alert_group"))
        within_ts = datetime.fromtimestamp(now - 90, tz=timezone.utc).isoformat()
        grp_data["created_at"] = within_ts
        grp_data["updated_at"] = within_ts
        r.set(f"incident:{inc1}:alert_group", json.dumps(grp_data))
        state_data = json.loads(r.get(f"incident:{inc1}:state"))
        state_data["state"] = "REMEDIATION"
        r.set(f"incident:{inc1}:state", json.dumps(state_data))

        # 3. 2-hop alert (frontend) recovers the open incident and merges (within fixed 180s window)
        inc2 = await correlator.process(
            {
                "groupKey": "real-pipe-frontend",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "RealTestFrontend500",
                            "service": "frontend",
                            "namespace": test_ns,
                            "severity": "warning",
                        },
                        "fingerprint": "fp-realpipe-fe-2",
                    }
                ],
            }
        )
        assert inc2 == inc1
        # Fixed window: created_at stayed anchored and was NOT reset on merge
        assert correlator.get_alert_group(inc1).created_at == datetime.fromisoformat(within_ts)

        # 4. Past 180s on a different service/alert creates a NEW incident (no runaway grouping)
        old_ts = datetime.fromtimestamp(now - 250, tz=timezone.utc).isoformat()
        grp_aged = json.loads(r.get(f"incident:{inc1}:alert_group"))
        grp_aged["created_at"] = old_ts
        r.set(f"incident:{inc1}:alert_group", json.dumps(grp_aged))
        inc3 = await correlator.process(
            {
                "groupKey": "real-pipe-checkout",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "RealTestCheckout500",
                            "service": "checkoutservice",
                            "namespace": test_ns,
                            "severity": "warning",
                        },
                        "fingerprint": "fp-realpipe-chk-3",
                    }
                ],
            }
        )
        assert inc3 is not None
        assert inc3 != inc1
        created_incidents.append(inc3)
    finally:
        # Clean up only testrealpipe keys from live Redis
        for pattern in (
            f"topology:{test_ns}:*",
            f"topology:edges:{test_ns}",
            f"topology:edge:{test_ns}:*",
            f"active:namespace:{test_ns}",
            f"recurrence:{test_ns}:*",
            f"recurrence:history:{test_ns}:*",
            "dedup:fp-realpipe-*",
            "flap:*fp-realpipe-*",
        ):
            for key in list(r.scan_iter(pattern)):
                r.delete(key)
        r.srem("topology:namespaces", test_ns)
        for inc_id in created_incidents:
            for key in list(r.scan_iter(f"incident:{inc_id}*")):
                r.delete(key)


@pytest.mark.asyncio
async def test_real_prometheus_tempo_runtime_discovery_and_api_health():
    """Verify live API, Prometheus, and runtime trace topology query work cleanly."""
    _require_http(f"{API_URL}/v1/health", "Aegis API")
    _require_http(f"{PROMETHEUS_URL}/-/healthy", "Prometheus")

    nodes, edges = await discover_runtime_trace_topology(
        namespaces=["boutique"],
        mcp_endpoints={
            "prometheus": PROMETHEUS_URL,
            "tempo": TEMPO_URL,
        },
        existing_nodes=[],
    )
    assert isinstance(nodes, list)
    assert isinstance(edges, list)


@pytest.mark.asyncio
async def test_real_qdrant_otel_demo_runbooks_retrieval():
    """Query live Qdrant + Ollama collection aegis_runbooks_otel_demo for updated runbook chunks."""
    _require_http(f"{QDRANT_URL}/collections/aegis_runbooks_otel_demo", "Qdrant")
    _require_http(f"{OLLAMA_URL}/api/tags", "Ollama")

    flag_res = await qdrant_search(
        query="cartFailure feature flag flagd toggle_feature_flag checkout freeze",
        collection="aegis_runbooks_otel_demo",
        top_k=3,
        qdrant_url=QDRANT_URL,
        ollama_url=OLLAMA_URL,
    )
    flag_texts = " ".join(
        " ".join(
            [
                str(r.get("content", "")),
                str(r.get("title", "")),
                str(r.get("summary", "")),
                str(r.get("section", "")),
            ]
        )
        for r in flag_res.get("results", [])
    )
    assert flag_res.get("results"), "Expected non-empty Qdrant results for OTel feature flags"
    assert "cartFailure" in flag_texts
    assert "toggle_feature_flag" in flag_texts

    ad_res = await qdrant_search(
        query="adHighCpu ad deployment High CPU manual GC",
        collection="aegis_runbooks_otel_demo",
        top_k=3,
        qdrant_url=QDRANT_URL,
        ollama_url=OLLAMA_URL,
    )
    ad_texts = " ".join(
        " ".join(
            [
                str(r.get("content", "")),
                str(r.get("title", "")),
                str(r.get("summary", "")),
                str(r.get("section", "")),
            ]
        )
        for r in ad_res.get("results", [])
    )
    assert ad_res.get("results"), "Expected non-empty Qdrant results for OTel ad service CPU"
    assert "adHighCpu" in ad_texts


@pytest.mark.asyncio
async def test_real_env_full_pipeline_merge_qdrant_and_prom_convergence(monkeypatch):
    """Full-order test: Phase 1 -> merge -> Phase 2 -> real Qdrant & live Prometheus convergence."""
    _require_http(f"{QDRANT_URL}/collections/aegis_runbooks_otel_demo", "Qdrant")
    _require_http(f"{OLLAMA_URL}/api/tags", "Ollama")
    _require_http(f"{PROMETHEUS_URL}/-/healthy", "Prometheus")

    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("OLLAMA_URL", OLLAMA_URL)

    # 1. Phase 1 findings: 1 grounded (flagd) + 1 skipped-tools (must remain explicit, not dropped)
    f_cfg = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Feature flag cartFailure enabled causing cart failures",
        confidence=0.91,
        evidence=[
            EvidenceItem(
                source_tool="get_feature_flag",
                query_or_cmd="get_feature_flag()",
                raw_output='{"cartFailure": {"defaultVariant": "on"}}',
            )
        ],
        verbatim_signals={"flag_name": "cartFailure"},
        recommended_actions=["toggle_feature_flag cartFailure false"],
    )
    f_infra = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="[no tools called, 0 evidence] Skipped tool pass",
        confidence=0.40,
        evidence=[],
        verbatim_signals={"tool_execution_status": "no tools called, 0 evidence"},
    )
    merged = merge_findings([f_infra, f_cfg], incident_id="inc-real-e2e")
    assert len(merged.ranked_findings) == 2

    # 2. Real Qdrant search step -> Phase 2 ActionProposal JSON via FakeLLM
    captured_prompts: List[str] = []
    from langchain_core.messages import AIMessage

    proposal = ActionProposal(
        incident_id="inc-real-e2e",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable active cartFailure flag",
                pre_approved=True,
            )
        ],
        confidence=0.91,
        risk_assessment="Low risk toggle",
    )

    class TwoStepReActLLM(FakeLLM):
        def __init__(self):
            super().__init__(response_text=proposal.model_dump_json())
            self._calls = 0

        async def ainvoke(self, messages, **kwargs):
            self._calls += 1
            captured_prompts.append("\n".join(str(getattr(m, "content", m)) for m in messages))
            if self._calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "qdrant_search",
                            "args": {"query": "cartFailure feature flag flagd", "top_k": 2},
                            "id": "call-real-qdrant-1",
                        }
                    ],
                )
            return AIMessage(content=proposal.model_dump_json())

    class LivePromMCP:
        async def call_tool(self, name: str, arguments: Dict[str, Any]):
            if name != "prometheus_query":
                return {"error": f"unexpected tool {name}"}
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{PROMETHEUS_URL}/api/v1/query",
                    params={"query": arguments["query"]},
                )
                return resp.json()

    async def fast_sleep(_sec):
        return None

    async def real_prom_convergence(incident_id, affected_services, severity):
        # Execute real run_convergence_monitor against live Prometheus with short SLO window
        fast_cfg = SystemConfig(
            convergence=ConvergenceConfig(
                bake_time_seconds_stateless=0,
                bake_time_seconds_stateful=0,
                poll_interval_seconds=1,
                window_seconds=2,
                slo=ConvergenceSLOConfig(),
            )
        )
        return await run_convergence_monitor(
            incident_id=incident_id,
            affected_services=affected_services,
            severity=severity,
            is_stateful=False,
            mcp_client=LivePromMCP(),
            system_config=fast_cfg,
            sleep_fn=fast_sleep,
        )

    async def mock_write(tool_name, params):
        return {"status": "disabled", "tool": tool_name, "params": params}

    graph = create_remediation_graph(
        llm=TwoStepReActLLM(),
        write_executor_fn=mock_write,
        convergence_fn=real_prom_convergence,
    )
    pass1_state = await graph.ainvoke(
        {
            "incident_id": "inc-real-e2e",
            "action_id": "act-real-e2e",
            "severity": "P2",
            "affected_service": "cart",
            "affected_namespace": "otel-demo",
            "merged_finding": merged.model_dump(mode="json"),
            "retry_count": 0,
        }
    )

    # Assertions: real Qdrant retrieved cartFailure runbook & Phase 2 got all ranked agents
    assert pass1_state.get("retrieved_runbooks"), "Phase 2 should retrieve real Qdrant chunks"
    assert any("cartFailure" in str(rb) for rb in pass1_state["retrieved_runbooks"])
    assert "no tools called, 0 evidence" in captured_prompts[0]
    assert pass1_state["outcome"] == "action_proposed"

    # Pass 2: operator approval executes action and runs live Prometheus convergence
    final_state = await graph.ainvoke(
        {
            "incident_id": "inc-real-e2e",
            "action_id": pass1_state.get("action_id") or "act-real-e2e",
            "action_proposal": pass1_state["action_proposal"],
            "approval_status": "approved",
            "affected_service": "cart",
            "affected_namespace": "otel-demo",
            "severity": "P2",
            "retry_count": 0,
        }
    )
    assert final_state["outcome"] in ("signals_converged", "signals_not_converged")

