"""Unit tests for Phase 1 merge logic (services/phase1_worker/merge.py).

Zero external dependencies; tests pure aggregation and corroboration ranking.
"""

from datetime import datetime, timezone
import pytest

from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding, EvidenceItem, MergedFinding
from services.phase1_worker.merge import merge_findings, score_evidence_corroboration


def test_merge_empty_findings():
    assert score_evidence_corroboration([]) == []
    result = merge_findings([], incident_id="inc-empty")
    assert isinstance(result, MergedFinding)
    assert result.incident_id == "inc-empty"
    assert result.merged_confidence == 0.0
    assert result.all_evidence == []
    assert result.findings_by_agent == {}


def test_merge_single_agent():
    finding = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="Pod cartservice OOMKilled",
        confidence=0.85,
        evidence=[
            EvidenceItem(
                source_tool="k8s_get_events",
                query_or_cmd="cartservice",
                raw_output="BackOff: restarting failed container",
            )
        ],
        infra_flags={"oom_killed": True, "node_eviction": False},
        verbatim_signals={"last_exit_code": 137},
    )

    result = merge_findings([finding], incident_id="inc-100")
    assert result.incident_id == "inc-100"
    assert result.primary_hypothesis == "Pod cartservice OOMKilled"
    assert result.infra_flags["oom_killed"] is True
    assert result.verbatim_signals["last_exit_code"] == 137
    assert len(result.all_evidence) == 1
    assert result.all_evidence[0].raw_output == "BackOff: restarting failed container"


def test_merge_corroboration_ranking():
    # Agent 1 reports cartservice OOMKilled
    a1 = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="cartservice memory pressure OOMKilled",
        confidence=0.8,
        evidence=[
            EvidenceItem(
                source_tool="k8s_get_pod_status",
                query_or_cmd="cartservice",
                raw_output="Pod status: CrashLoopBackOff due to OOMKilled in cartservice",
            ),
            EvidenceItem(
                source_tool="prometheus_query",
                query_or_cmd="node_memory_usage",
                raw_output="Node ip-10-0-1 memory usage 72%",
            ),
        ],
        infra_flags={"oom_killed": True},
    )

    # Agent 2 reports cartservice java OutOfMemoryError in logs
    a2 = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="cartservice java heap OutOfMemoryError stack trace",
        confidence=0.9,
        evidence=[
            EvidenceItem(
                source_tool="loki_query",
                query_or_cmd='{app="cartservice"} |= "OutOfMemoryError"',
                raw_output="java.lang.OutOfMemoryError: GC overhead limit exceeded in cartservice",
            ),
            EvidenceItem(
                source_tool="tempo_search",
                query_or_cmd='service="frontend"',
                raw_output="trace frontend 500 error calling cartservice",
            ),
        ],
    )

    # Agent 3 reports unrelated change in paymentservice
    a3 = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="No recent commits to cartservice; paymentservice updated 2h ago",
        confidence=0.6,
        evidence=[
            EvidenceItem(
                source_tool="github_get_commits",
                query_or_cmd="paymentservice",
                raw_output="commit abc1234: update payment gateway",
            )
        ],
    )

    result = merge_findings([a1, a2, a3], incident_id="inc-200")
    
    # Assert primary hypothesis is from highest confidence agent (a2)
    assert result.primary_hypothesis == a2.hypothesis

    # Corroborated evidence mentioning cartservice should be ranked higher than isolated paymentservice
    assert len(result.all_evidence) == 5
    top_items = result.all_evidence[:2]
    # Check that top items mention cartservice
    assert any("cartservice" in (item.raw_output.lower() + item.query_or_cmd.lower()) for item in top_items)
    
    # Paymentservice evidence should be at or near bottom
    last_item = result.all_evidence[-1]
    assert "paymentservice" in last_item.query_or_cmd or "ip-10-0-1" in last_item.raw_output


def test_merge_verbatim_evidence():
    exact_stack_trace = """Exception in thread "main" java.lang.OutOfMemoryError: Java heap space
    at com.boutique.cartservice.CartService.addItem(CartService.java:142)
    at com.boutique.cartservice.Main.main(Main.java:58)"""

    a1 = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Memory leak in cartservice",
        confidence=0.95,
        evidence=[
            EvidenceItem(
                source_tool="loki_query",
                query_or_cmd="app=cartservice",
                raw_output=exact_stack_trace,
            )
        ],
        verbatim_signals={"exact_error": exact_stack_trace},
    )

    result = merge_findings([a1], incident_id="inc-300")
    assert result.all_evidence[0].raw_output == exact_stack_trace
    assert result.verbatim_signals["exact_error"] == exact_stack_trace


def test_extract_tokens_edge_cases():
    from services.phase1_worker.merge import _extract_tokens

    assert _extract_tokens("") == set()
    assert _extract_tokens(None) == set()
    # Stop words are filtered
    tokens = _extract_tokens("the and for with status error warning")
    assert tokens == set()
    # Non-stop words extracted
    tokens_real = _extract_tokens("cartservice OOMKilled memory pressure")
    assert "cartservice" in tokens_real
    assert "oomkilled" in tokens_real


def test_merge_priority_tie_breaking():
    # Tie between Infrastructure and LogTrace at same confidence (0.85)
    a1 = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="A1 Infra hypothesis",
        confidence=0.85,
        evidence=[],
        verbatim_signals={},
    )
    a2 = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="A2 Log hypothesis",
        confidence=0.85,
        evidence=[],
        verbatim_signals={},
    )
    result = merge_findings([a2, a1], incident_id="inc-tie")
    # Infrastructure (priority 3) should win tie over Log (priority 2)
    assert result.primary_hypothesis == "A1 Infra hypothesis"


def test_merge_infra_flags_fallback_from_non_infra_agent():
    # Only ChangeConfig agent present, reporting an infra flag
    a3 = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Config changed",
        confidence=0.7,
        evidence=[],
        verbatim_signals={},
        infra_flags={"restarted_deployment": True},
    )
    result = merge_findings([a3], incident_id="inc-a3-flags")
    assert result.infra_flags["restarted_deployment"] is True


def test_merge_ranks_and_preserves_zero_tool_findings():
    """All agents are preserved in ranked order; zero-tool findings stay explicit rather than dropped."""
    from shared.models.findings import EvidenceItem

    a_infra_zero = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="[no tools called, 0 evidence] Model skipped tools",
        confidence=0.4,
        evidence=[],
        verbatim_signals={"tool_execution_status": "no tools called, 0 evidence"},
    )
    a_cfg_grounded = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Flag cartFailure switched to 100%",
        confidence=0.85,
        evidence=[
            EvidenceItem(
                source_tool="get_feature_flag",
                query_or_cmd="get_feature_flag(flag_name='cartFailure')",
                raw_output='{"defaultVariant": "100%"}',
            )
        ],
        verbatim_signals={"flag_name": "cartFailure"},
    )
    a_log_empty = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Unverified log note",
        confidence=0.5,
        evidence=[],
        verbatim_signals={},
    )

    result = merge_findings([a_infra_zero, a_log_empty, a_cfg_grounded], incident_id="inc-rank-all")

    assert len(result.ranked_findings) == 3
    assert result.ranked_findings[0].agent_role == AgentRole.CHANGE_CONFIG_INVESTIGATOR
    # Zero-tool finding stays in both ranked_findings and findings_by_agent
    roles_in_order = [f.agent_role for f in result.ranked_findings]
    assert AgentRole.INFRASTRUCTURE_INVESTIGATOR in roles_in_order
    assert AgentRole.LOG_TRACE_INVESTIGATOR in roles_in_order
    assert AgentRole.INFRASTRUCTURE_INVESTIGATOR.value in result.findings_by_agent
    assert (
        result.findings_by_agent[AgentRole.INFRASTRUCTURE_INVESTIGATOR.value].verbatim_signals.get("tool_execution_status")
        == "no tools called, 0 evidence"
    )


def test_merge_stratified_caps_preserve_low_confidence_agents():
    """MergedFinding preserves all findings/evidence undropped with stratified per-role ranking at the top."""
    from shared.models.findings import EvidenceItem

    a_infra = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="Infra pod crash",
        confidence=0.95,
        evidence=[
            EvidenceItem(source_tool="k8s_get_pod_status", query_or_cmd=f"q-k8s-{i}", raw_output=f"pod fail {i}")
            for i in range(6)
        ],
    )
    a_log = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Log timeout burst",
        confidence=0.80,
        evidence=[
            EvidenceItem(source_tool="loki_query", query_or_cmd="q-loki-1", raw_output="timeout err"),
            EvidenceItem(source_tool="loki_query", query_or_cmd="q-loki-2", raw_output="retry err"),
            EvidenceItem(source_tool="tempo_search", query_or_cmd="q-tempo-1", raw_output="span 500"),
            EvidenceItem(source_tool="jaeger_search", query_or_cmd="q-jaeger-1", raw_output="span err"),
        ],
    )
    # Low-confidence specialist must NOT be dropped
    a_cfg_low = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Subtle flag change",
        confidence=0.15,
        evidence=[
            EvidenceItem(source_tool="get_feature_flag", query_or_cmd="get_feature_flag()", raw_output="cartFailure=on"),
        ],
    )

    result = merge_findings([a_infra, a_log, a_cfg_low], incident_id="inc-cap-1")

    assert len(result.ranked_findings) == 3
    assert AgentRole.CHANGE_CONFIG_INVESTIGATOR in result.findings_by_agent
    # None dropped in MergedFinding: all 6+4+1 = 11 unique items preserved
    assert len(result.findings_by_agent[AgentRole.INFRASTRUCTURE_INVESTIGATOR].evidence) == 6
    assert len(result.all_evidence) == 11
    # Top 5 stratified evidence includes the low-confidence agent's get_feature_flag call
    assert any(e.source_tool == "get_feature_flag" for e in result.all_evidence[:5])
