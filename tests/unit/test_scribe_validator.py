"""Unit tests for the deterministic report validation engine."""

from datetime import datetime, timezone
import pytest

from services.phase3_worker.validator import (
    validate_incident_report,
    validate_sections,
    validate_scorecard,
    validate_root_cause_hypothesis,
    validate_timeline_grounding,
    validate_actions_and_denials,
)
from shared.models.enums import IncidentState
from shared.models.ledger import LedgerEntry


@pytest.fixture
def sample_ledger_entries():
    now = datetime.now(timezone.utc)
    return [
        LedgerEntry(
            entry_id="evt-1",
            incident_id="inc-oom-1",
            timestamp=now,
            state=IncidentState.TRIAGE,
            event_type="alert_received",
            source="alertmanager",
            details={"service": "cartservice"},
        ),
        LedgerEntry(
            entry_id="evt-2",
            incident_id="inc-oom-1",
            timestamp=now,
            state=IncidentState.REMEDIATION,
            event_type="triage_complete",
            source="phase1_worker",
            details={"hypothesis": "Memory leak"},
        ),
        LedgerEntry(
            entry_id="evt-3",
            incident_id="inc-oom-1",
            timestamp=now,
            state=IncidentState.EXECUTING,
            event_type="execution_success",
            source="phase2_worker",
            details={"action": "restart_pod"},
        ),
        LedgerEntry(
            entry_id="evt-4",
            incident_id="inc-oom-1",
            timestamp=now,
            state=IncidentState.REPORTING,
            event_type="signals_converged",
            source="convergence_monitor",
            details={"converged": True},
        ),
    ]


@pytest.fixture
def valid_report_markdown():
    return """# Incident Report — inc-oom-1

## Summary
- **Severity:** P2
- **Duration:** 10:00:00 → 10:05:00 (300.0s)
- **Affected Services:** cartservice.boutique
- **Root Cause:** Suspected memory leak in cartservice due to cache accumulation

## Timeline
| Time | State | Event | Source | Details |
|---|---|---|---|---|
| 10:00:00 | TRIAGE | alert_received | alertmanager | Alert fired |
| 10:02:00 | REMEDIATION | triage_complete | phase1_worker | Investigation done |
| 10:04:00 | EXECUTING | execution_success | phase2_worker | Pod restarted |
| 10:05:00 | REPORTING | signals_converged | convergence_monitor | Healthy bounds restored |

## Investigation Findings
- `metric`: **Memory pressure peak at 98.4%** (peak: 98.4%)

## Actions Taken
- `restart_pod` on `cartservice` — outcome: completed

## Recovery Confirmation
- Golden signals converged at: 10:05:00 UTC
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
        "duration_seconds": 300.0,
        "root_cause_summary": "Suspected memory leak",
        "remediation_summary": "Restarted pod",
        "timeline": [],
        "action_log": [{"tool": "restart_pod", "outcome": "completed"}],
        "preventative_recommendations": ["Review memory limits"],
    }


def test_validate_incident_report_happy_path(valid_report_markdown, sample_ledger_entries, valid_report_dict):
    """Assert valid report passes all 6 checks."""
    result = validate_incident_report(
        markdown_text=valid_report_markdown,
        ledger_entries=sample_ledger_entries,
        report_data=valid_report_dict,
    )
    assert result.is_valid is True
    assert len(result.errors) == 0
    assert result.validated_report is not None
    assert result.validated_report.incident_id == "inc-oom-1"


def test_validate_sections_missing_heading(valid_report_markdown):
    """Assert omitting a required section triggers failure."""
    bad_markdown = valid_report_markdown.replace("## Recommendations\n- Review memory limits and configure automated alerts.", "")
    errors = validate_sections(bad_markdown)
    assert len(errors) == 1
    assert "Recommendations" in errors[0]


def test_validate_scorecard_rejects_prefilled_checkbox(valid_report_markdown):
    """Assert pre-filling any scorecard checkbox triggers failure."""
    prefilled_markdown = valid_report_markdown.replace("[ ] Correct", "[x] Correct")
    errors = validate_scorecard(prefilled_markdown)
    assert len(errors) == 1
    assert "UNFILLED" in errors[0]


def test_validate_scorecard_missing_kpi(valid_report_markdown):
    """Assert scorecard missing a KPI triggers failure."""
    missing_kpi_md = valid_report_markdown.replace("1. Root Cause Accuracy (RCIA)", "1. General Triage Score")
    errors = validate_scorecard(missing_kpi_md)
    assert len(errors) == 1
    assert "Root Cause Accuracy" in errors[0]


def test_validate_timeline_rejects_hallucinated_event(valid_report_markdown, sample_ledger_entries):
    """Assert timeline table rows with events not in the ledger are flagged."""
    hallucinated_row = "| 10:03:00 | CHAOS | cluster_node_terminated | chaos_mesh | Node crashed |\n"
    bad_md = valid_report_markdown.replace(
        "| 10:04:00 | EXECUTING | execution_success | phase2_worker | Pod restarted |\n",
        hallucinated_row,
    )
    errors = validate_timeline_grounding(bad_md, sample_ledger_entries)
    assert len(errors) >= 1
    assert "contains events or transitions not found in the incident ledger" in errors[0]


def test_validate_root_cause_rejects_absolute_fact(valid_report_markdown):
    """Assert stating root cause as absolute fact without hypothesis triggers failure."""
    bad_md = valid_report_markdown.replace(
        "Suspected memory leak in cartservice due to cache accumulation",
        "The developer deployed a bug that caused cartservice to fail",
    )
    errors = validate_root_cause_hypothesis(bad_md)
    assert len(errors) == 1
    assert "absolute fact" in errors[0]


def test_validate_actions_rejects_omitted_denial(valid_report_markdown, sample_ledger_entries):
    """Assert when a denial is in the ledger, omitting it from Actions Taken triggers failure."""
    denied_entries = list(sample_ledger_entries)
    denied_entries.append(
        LedgerEntry(
            entry_id="evt-denied",
            incident_id="inc-oom-1",
            timestamp=datetime.now(timezone.utc),
            state=IncidentState.ESCALATED,
            event_type="denied",
            source="telegram",
            details={"denied_by": "oncall_eng"},
        )
    )
    errors = validate_actions_and_denials(valid_report_markdown, denied_entries)
    assert len(errors) == 1
    assert "denied" in errors[0].lower()
