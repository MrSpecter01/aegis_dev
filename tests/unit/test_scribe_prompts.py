"""Unit tests for Scribe Agent prompt templates and invariants."""

from pathlib import Path
import pytest

from services.phase3_worker.graph import load_scribe_prompts
from services.phase3_worker.validator import SCORECARD_KPIS, REQUIRED_SECTIONS


def test_incident_report_template_sections_and_placeholders():
    """Verify incident_report_template.md contains all 7 sections and required placeholders."""
    _, template = load_scribe_prompts()

    # Assert 7 required sections are defined
    assert "# Incident Report — {incident_id}" in template
    assert "## Summary" in template
    assert "## Timeline" in template
    assert "## Investigation Findings" in template
    assert "## Actions Taken" in template
    assert "## Recovery Confirmation" in template
    assert "## Recommendations" in template
    assert "## Scorecard (to be filled by team)" in template

    # Assert required placeholders exist
    expected_placeholders = [
        "{incident_id}",
        "{severity}",
        "{start_time}",
        "{resolved_time}",
        "{duration}",
        "{affected_services}",
        "{root_cause_hypothesis}",
        "{timeline_table}",
        "{ranked_evidence_summary}",
        "{action_plan_with_outcomes}",
        "{convergence_timestamp}",
        "{signals_summary}",
        "{recommendations}",
        "{triage_duration}",
        "{recovery_duration}",
    ]
    for ph in expected_placeholders:
        assert ph in template, f"Placeholder '{ph}' missing from incident_report_template.md"


def test_incident_report_template_scorecard_has_all_kpis_unfilled():
    """Verify that template contains all 6 KPIs and zero filled checkboxes."""
    _, template = load_scribe_prompts()

    for kpi in SCORECARD_KPIS:
        assert kpi.lower() in template.lower(), f"Scorecard KPI '{kpi}' missing from template"

    # Must contain unfilled checkboxes [ ]
    assert "[ ] Correct" in template
    assert "[ ] Full Resolution" in template
    assert "[ ] 100% Grounded" in template
    assert "[ ] 100% Policy Compliant" in template
    assert "[ ] Fully Autonomous" in template

    # Must not contain any checked box
    assert "[x]" not in template.lower()
    assert "[✓]" not in template


def test_system_scribe_agent_prompt_grounding_rules():
    """Verify system_scribe_agent.md enforces ledger derivation, hypothesis, and gap markers."""
    sys_prompt, _ = load_scribe_prompts()

    # Grounding & hypothesis directives
    assert "Derive All Content Strictly from Ledger and Findings" in sys_prompt
    assert "Root Cause Must Be a Hypothesis" in sys_prompt
    assert "Verbatim Telemetry and Evidence Preservation" in sys_prompt
    assert "[UNKNOWN — no ledger entry]" in sys_prompt
    assert "Scorecard Section Must Be Completely Unfilled" in sys_prompt

    # Missing data & negative conditions directives
    assert "Strict Handling of Missing, Incomplete, or Unobserved Data Across All Dimensions" in sys_prompt
    assert "Zero Speculation / Zero Invention Rule" in sys_prompt
    assert "Admitting Ignorance is Rewarded" in sys_prompt

