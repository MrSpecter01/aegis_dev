"""Unit tests for the LangGraph Scribe reasoning graph and delivery flow."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.phase3_worker.graph import (
    create_scribe_graph,
    format_timeline_table,
    ScribeGraphState,
)
from shared.models.enums import IncidentState
from shared.models.ledger import LedgerEntry
from tests.helpers.fake_llm import FakeLLM


@pytest.fixture
def sample_ledger():
    base_time = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    return [
        LedgerEntry(
            entry_id="evt-1",
            incident_id="inc-test-01",
            timestamp=base_time,
            state=IncidentState.TRIAGE,
            event_type="alert_received",
            source="alertmanager",
            details={"service": "cartservice"},
        ),
        LedgerEntry(
            entry_id="evt-2",
            incident_id="inc-test-01",
            timestamp=base_time + timedelta(seconds=60),
            state=IncidentState.REMEDIATION,
            event_type="triage_complete",
            source="phase1_worker",
            details={"hypothesis": "Memory leak"},
        ),
        LedgerEntry(
            entry_id="evt-3",
            incident_id="inc-test-01",
            timestamp=base_time + timedelta(seconds=120),
            state=IncidentState.EXECUTING,
            event_type="execution_success",
            source="phase2_worker",
            details={"action": "restart_pod"},
        ),
        LedgerEntry(
            entry_id="evt-4",
            incident_id="inc-test-01",
            timestamp=base_time + timedelta(seconds=180),
            state=IncidentState.REPORTING,
            event_type="signals_converged",
            source="convergence_monitor",
            details={"converged": True},
        ),
    ]


@pytest.fixture
def valid_report_response():
    return """# Incident Report — inc-test-01

## Summary
- **Severity:** P2
- **Duration:** 10:00:00 → 10:03:00 (180.0s)
- **Affected Services:** cartservice.boutique
- **Root Cause:** Suspected memory leak in cartservice due to cache accumulation

## Timeline
| Time | State | Event | Source | Details |
|---|---|---|---|---|
| 10:00:00 | TRIAGE | alert_received | alertmanager | Alert fired |
| 10:01:00 | REMEDIATION | triage_complete | phase1_worker | Investigation done |
| 10:02:00 | EXECUTING | execution_success | phase2_worker | Pod restarted |
| 10:03:00 | REPORTING | signals_converged | convergence_monitor | Healthy bounds restored |

## Investigation Findings
- `metric`: **Memory pressure peak at 98.4%** (peak: 98.4%)

## Actions Taken
- `restart_pod` on `cartservice` — outcome: completed

## Recovery Confirmation
- Golden signals converged at: 10:03:00 UTC
- Signals checked: Error rate < 0.01, Latency < 500ms

## Recommendations
- Review memory limits and configure automated alerts.

## Scorecard (to be filled by team)

| Dimension | Metric / Evaluation Criteria | Assessment (Select One) |
|---|---|---|
| **1. Root Cause Accuracy (RCIA)** | Isolated true root cause? | [ ] Correct [ ] Partial [ ] Incorrect |
| **2. Fix Effectiveness (FTFR)** | Resolved on first attempt? | [ ] Full Resolution [ ] Partial [ ] No Effect |
| **3. Resolution Latency (MTTT/MTTR)** | Triage: 45s | [ ] Target SLA Met [ ] SLA Breached |
| **4. Evidence Grounding** | Timeline events verified? | [ ] 100% Grounded [ ] Gaps Marked [ ] Hallucinations |
| **5. Action Safety & Compliance** | SAG policy compliant? | [ ] 100% Policy Compliant [ ] Violation |
| **6. Human Intervention Rate (HIR)** | Level of human intervention | [ ] Fully Autonomous [ ] Steered [ ] Escalated |

- **Reviewer:** ___________________________
- **Review Date:** ___________________________
- **Action Items & Preventative Notes:** ___________________________
"""


def test_format_timeline_table_gap_detection():
    """Verify format_timeline_table detects time jumps > 300s."""
    t0 = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=600)  # 10 min gap
    entries = [
        LedgerEntry(entry_id="1", incident_id="inc-1", timestamp=t0, state=IncidentState.TRIAGE, event_type="alert_received", source="src"),
        LedgerEntry(entry_id="2", incident_id="inc-1", timestamp=t1, state=IncidentState.REMEDIATION, event_type="triage_complete", source="src"),
    ]
    table, gaps = format_timeline_table(entries)
    assert len(gaps) == 1
    assert "600s gap" in gaps[0]
    assert "[UNKNOWN — no ledger entry" in table


@pytest.mark.asyncio
async def test_scribe_graph_full_flow(sample_ledger, valid_report_response, tmp_path):
    """Test full LangGraph execution from load_ledger to deliver_report."""
    fake_llm = FakeLLM(response_text=valid_report_response)
    mock_delivery = AsyncMock(return_value={"report_file": str(tmp_path / "report.md"), "transition_successful": True})

    graph = create_scribe_graph(llm=fake_llm, delivery_fn=mock_delivery)

    initial_state: ScribeGraphState = {
        "incident_id": "inc-test-01",
        "severity": "P2",
        "affected_service": "cartservice",
        "affected_namespace": "boutique",
        "ledger_entries": sample_ledger,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["is_valid"] is True
    assert final_state["incident_report"] is not None
    assert final_state["incident_report"].incident_id == "inc-test-01"
    mock_delivery.assert_called_once()


@pytest.mark.asyncio
async def test_scribe_graph_reflection_loop_on_validation_failure(sample_ledger, valid_report_response, tmp_path):
    """Test that graph reflects and re-generates when initial validation fails."""
    # First response has a checked box (invalid); second response is valid
    invalid_response = valid_report_response.replace("[ ] Correct", "[x] Correct")

    call_count = 0

    class SequentialLLM:
        def invoke(self, messages):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(content=invalid_response)
            return MagicMock(content=valid_report_response)

        async def ainvoke(self, messages):
            return self.invoke(messages)

    mock_delivery = AsyncMock(return_value={"transition_successful": True})
    graph = create_scribe_graph(llm=SequentialLLM(), delivery_fn=mock_delivery)

    initial_state: ScribeGraphState = {
        "incident_id": "inc-test-01",
        "severity": "P2",
        "affected_service": "cartservice",
        "affected_namespace": "boutique",
        "ledger_entries": sample_ledger,
    }

    final_state = await graph.ainvoke(initial_state)

    # Assert reflection occurred: LLM invoked twice, second attempt passed
    assert call_count == 2
    assert final_state["is_valid"] is True
    mock_delivery.assert_called_once()


@pytest.fixture
def valid_report_dict():
    now = datetime.now(timezone.utc)
    return {
        "incident_id": "inc-oom-1",
        "title": "Incident inc-oom-1 — cartservice",
        "severity": "P2",
        "affected_service": "cartservice",
        "affected_namespace": "boutique",
        "start_time": now,
        "end_time": now,
        "duration_seconds": 180.0,
        "root_cause_summary": "Suspected memory leak",
        "remediation_summary": "Restarted pod",
        "timeline": [],
        "action_log": [{"tool": "restart_pod", "outcome": "completed"}],
        "preventative_recommendations": ["Review memory limits"],
    }


@pytest.mark.asyncio
async def test_delivery_persists_report_and_notifies_api_without_direct_telegram(sample_ledger, valid_report_dict, valid_report_response, tmp_path):
    """Assert deliver_report persists report to disk and notifies API without making direct Telegram requests."""
    from shared.models.ledger import IncidentReport
    from services.phase3_worker.delivery import deliver_report

    valid_report_dict["markdown_content"] = valid_report_response
    report = IncidentReport.model_validate(valid_report_dict)

    with patch("services.phase3_worker.delivery.notify_api_state_transition", new_callable=AsyncMock) as mock_notify, \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_http_post:
        mock_notify.return_value = True

        result = await deliver_report(
            incident_id="inc-oom-1",
            report=report,
            ledger_entries=sample_ledger,
            storage_path=str(tmp_path),
        )

        assert result["transition_successful"] is True
        # Verify file exists on disk
        assert (tmp_path / "inc-oom-1" / "report.md").is_file()
        # Verify notify_api_state_transition was called with report_path
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["event"] == "report_delivered"
        assert "inc-oom-1" in call_kwargs["metadata"]["report_path"]
        assert call_kwargs["metadata"]["delivered_to"] == ["disk"]
        # Verify no direct HTTP calls made to telegram.org
        for call in mock_http_post.call_args_list:
            assert "api.telegram.org" not in str(call[0][0])


@pytest.mark.asyncio
async def test_scribe_renders_no_tools_called_without_dropping(sample_ledger, valid_report_response):
    """Ensure Scribe prompt includes full ranked findings and explicit no-tools status."""
    from services.phase1_worker.merge import merge_findings
    from shared.models.enums import AgentRole
    from shared.models.findings import AgentFinding, EvidenceItem

    captured = []

    class CapturingScribeLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            captured.append(
                "\n".join(
                    str(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", m))
                    for m in messages
                )
            )
            return await super().ainvoke(messages, **kwargs)

    f_cfg = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Flag cartFailure=on",
        confidence=0.9,
        evidence=[
            EvidenceItem(
                source_tool="get_feature_flag",
                query_or_cmd="get_feature_flag()",
                raw_output="on",
            )
        ],
    )
    f_infra = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="[no tools called, 0 evidence] Model skipped tools",
        confidence=0.4,
        evidence=[],
        verbatim_signals={"tool_execution_status": "no tools called, 0 evidence"},
    )
    merged = merge_findings([f_infra, f_cfg], incident_id="inc-test-01")

    llm = CapturingScribeLLM(response_text=valid_report_response)
    graph = create_scribe_graph(llm=llm, delivery_fn=AsyncMock(return_value={"delivered": True}))

    state: ScribeGraphState = {
        "incident_id": "inc-test-01",
        "severity": "P2",
        "affected_service": "cartservice",
        "affected_namespace": "boutique",
        "ledger_entries": sample_ledger,
        "context": merged.model_dump(mode="json"),
        "reflection_attempts": 0,
    }

    await graph.ainvoke(state)

    assert len(captured) >= 1
    assert "no tools called, 0 evidence" in captured[0]
    assert "get_feature_flag" in captured[0]
    assert "Rank 1" in captured[0]
    assert "Rank 2" in captured[0]
