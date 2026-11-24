import pytest
from datetime import datetime, timezone

from shared.models import (
    IncidentState,
    Severity,
    AgentRole,
    Alert,
    AlertGroup,
    TopologyNode,
    SubGraph,
    AgentFinding,
    EvidenceItem,
    MergedFinding,
    ActionPlanItem,
    ActionProposal,
    ApprovalRequest,
    ApprovalDecision,
    LedgerEntry,
    IncidentReport,
)


def test_alert_model_properties():
    alert = Alert(
        labels={
            "alertname": "PodMemoryHigh",
            "namespace": "boutique",
            "pod": "cartservice-7d4847994-x9z2l",
            "severity": "P2",
        },
        annotations={"summary": "Memory saturation"},
    )
    assert alert.alertname == "PodMemoryHigh"
    assert alert.namespace == "boutique"
    assert alert.service == "cartservice"
    assert alert.severity == Severity.P2


def test_alert_group_model():
    alert = Alert(labels={"alertname": "PodMemoryHigh", "service": "cartservice"})
    group = AlertGroup(
        group_id="grp-001",
        primary_alert=alert,
        severity=Severity.P2,
        affected_service="cartservice",
        affected_namespace="boutique",
    )
    assert group.group_id == "grp-001"
    assert group.severity == Severity.P2
    assert len(group.related_alerts) == 0


def test_topology_models():
    node = TopologyNode(
        id="cartservice.boutique",
        name="cartservice",
        namespace="boutique",
        type="Deployment",
        replicas=2,
        resource_limits={"cpu": "300m", "memory": "128Mi"},
    )
    assert node.id == "cartservice.boutique"
    assert node.replicas == 2
    assert node.stub is False

    subgraph = SubGraph(target_node=node)
    assert subgraph.target_node.name == "cartservice"


def test_findings_and_merged_findings():
    evidence = EvidenceItem(
        source_tool="prometheus_query",
        query_or_cmd="container_memory_working_set_bytes",
        raw_output="95% usage",
    )
    finding = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="OOMKilled risk due to memory leak",
        confidence=0.85,
        evidence=[evidence],
        infra_flags={"memory_pressure": True},
    )
    assert finding.confidence == 0.85
    assert finding.infra_flags["memory_pressure"] is True

    merged = MergedFinding(
        incident_id="inc-20260830-001",
        primary_hypothesis="Memory leak in cartservice",
        merged_confidence=0.88,
        findings_by_agent={AgentRole.INFRASTRUCTURE_INVESTIGATOR: finding},
        corroboration_score=0.90,
    )
    assert merged.incident_id == "inc-20260830-001"
    assert merged.corroboration_score == 0.90


def test_action_and_approval_models():
    item = ActionPlanItem(
        tool_name="restart_pod",
        target_resource="cartservice-7d4847994-x9z2l",
        rationale="Clear memory leak state",
    )
    proposal = ActionProposal(
        incident_id="inc-20260830-001",
        actions=[item],
        confidence=0.90,
        risk_assessment="Low risk - single replica pod restart",
    )
    request = ApprovalRequest(
        request_id="req-101",
        incident_id="inc-20260830-001",
        proposal=proposal,
        channel_notified=["telegram"],
    )
    assert request.proposal.actions[0].tool_name == "restart_pod"

    decision = ApprovalDecision(
        incident_id="inc-20260830-001",
        approved=True,
        decided_by="admin@example.com",
        channel="telegram",
    )
    assert decision.approved is True


def test_ledger_and_report_models():
    entry = LedgerEntry(
        entry_id="led-001",
        incident_id="inc-20260830-001",
        state=IncidentState.TRIAGE,
        event_type="agent_finished",
        source="phase1_worker",
        details={"agent": "infra"},
    )
    assert entry.state == IncidentState.TRIAGE

    report = IncidentReport(
        incident_id="inc-20260830-001",
        title="OOMKilled Incident cartservice",
        severity="P2",
        affected_service="cartservice",
        affected_namespace="boutique",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        duration_seconds=120.0,
        root_cause_summary="Memory leak in cartservice",
        remediation_summary="Restarted pod and scaled replicas",
    )
    assert report.incident_id == "inc-20260830-001"


def test_runbook_citation_normalization_on_action_proposal():
    from shared.models.actions import RunbookCitation

    cit = RunbookCitation.model_validate(
        {
            "id": "feature-flag-failure",
            "title": "Feature Flag Failure Mitigation",
            "section": "Remediation Steps",
            "line_start": 46,
            "line_end": 49,
            "chunk_index": 4,
            "total_chunks": 5,
            "excerpt": "Call toggle_feature_flag with enabled: false",
        }
    )
    assert cit.runbook_id == "feature-flag-failure"
    assert cit.book_name == "Feature Flag Failure Mitigation"
    assert cit.section == "Remediation Steps"
    assert cit.line_range == "L46-L49"
    assert cit.chunk_id == "chunk 4/5"
    assert "toggle_feature_flag" in cit.referenced_text

    proposal = ActionProposal(
        incident_id="inc-cit-01",
        rationale="Disable active failure toggle per Feature Flag Failure Mitigation.",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable cartFailure flag.",
                runbook_citations=[cit],
            )
        ],
        runbook_citations=[cit],
        confidence=0.95,
        risk_assessment="Low risk",
    )
    dumped = proposal.model_dump(mode="json")
    assert dumped["runbook_citations"][0]["line_range"] == "L46-L49"
    assert dumped["actions"][0]["runbook_citations"][0]["chunk_id"] == "chunk 4/5"
