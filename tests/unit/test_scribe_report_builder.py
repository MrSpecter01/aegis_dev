"""Unit tests for deterministic Scribe report section builders.

Uses a real ledger (inc-20261102-0001: scale → re-scale → aborted by safety check →
rollback) so the builders are exercised against the production ledger shape.
"""

import json
from pathlib import Path
from typing import List

import pytest

from services.phase3_worker.report_builder import (
    build_actions_section,
    build_hypothesis_evolution,
    build_recovery_section,
    build_status_badge,
    build_timeline_narrative,
    build_timeline_table,
    collect_actions,
    collect_operator_inputs,
    compute_status,
    enforce_sections,
    humanize_details,
)
from shared.models.enums import IncidentState
from shared.models.ledger import LedgerEntry

FIXTURE = Path(__file__).parent.parent / "fixtures" / "ledger_replan_inc-20261102-0001.json"


@pytest.fixture
def entries() -> List[LedgerEntry]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [LedgerEntry.model_validate(r) for r in raw if r["event_type"] != "report_delivered"]


def test_timeline_table_column_order_event_before_state(entries):
    table, _ = build_timeline_table(entries)
    header = table.splitlines()[0]
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert cols == ["Time (UTC)", "Event", "State", "Source", "Details"]


def test_timeline_table_details_have_no_json_braces(entries):
    table, _ = build_timeline_table(entries)
    for row in table.splitlines()[2:]:
        details = row.strip("|").split("|")[4]
        assert "{" not in details and "}" not in details


def test_timeline_table_every_row_has_five_columns(entries):
    table, _ = build_timeline_table(entries)
    rows = table.splitlines()[2:]
    assert len(rows) == len(entries)
    for row in rows:
        cols = [c.strip() for c in row.strip("|").split("|")]
        assert len(cols) == 5


def test_humanize_details_action_proposed_is_plain_text(entries):
    proposed = next(e for e in entries if e.event_type == "action_proposed")
    text = humanize_details(proposed)
    assert "`scale_deployment` on `boutique/frontend`" in text
    assert "replicas=3" in text


def test_actions_section_lists_every_action_including_aborted(entries):
    records = collect_actions(entries)
    assert [r["tool"] for r in records] == ["scale_deployment", "scale_deployment", "rollback_release"]
    section = build_actions_section(entries)
    assert "### 1. `scale_deployment`" in section
    assert "### 2. `scale_deployment`" in section
    assert "### 3. `rollback_release`" in section
    assert "replicas=3" in section and "replicas=4" in section and "revision=0" in section
    assert "*aborted* before execution by safety check" in section
    assert "IMAGE_PULL_FAILURE" in section
    assert "executed — *success*" in section


def test_actions_section_records_approver_and_channel(entries):
    section = build_actions_section(entries)
    assert "approved by `eval-runner` via `dashboard`" in section


def test_actions_section_denied_action_is_reported():
    base = LedgerEntry(
        entry_id="1", incident_id="i", state=IncidentState.AWAITING_APPROVAL,
        event_type="action_proposed", source="phase2_worker",
        details={"action_id": "a1", "proposal": {"actions": [{"tool_name": "restart_pod", "target_resource": "ns/x", "parameters": {}, "rationale": "r"}], "confidence": 0.5, "risk_assessment": "low"}},
    )
    denied = LedgerEntry(
        entry_id="2", incident_id="i", state=IncidentState.ESCALATED, event_type="denied",
        source="alice", details={"action_id": "a1", "channel": "telegram", "reason": "too risky"},
    )
    section = build_actions_section([base, denied])
    assert "denied by `alice` via `telegram`" in section
    assert "too risky" in section
    assert "not executed" in section


def test_actions_section_empty_ledger_marks_no_record():
    assert "[no record]" in build_actions_section([])


def test_status_badge_resolved_with_replans(entries):
    badge, text = compute_status(entries)
    assert badge == "✅ RESOLVED"
    assert "revised" in text
    res = build_status_badge(entries)
    assert res.startswith("> **STATUS: ✅ RESOLVED**")
    assert f"<sub>{text}</sub>" in res


def test_status_badge_escalated_when_denied():
    e = LedgerEntry(entry_id="1", incident_id="i", state=IncidentState.ESCALATED, event_type="denied", source="bob")
    assert compute_status([e])[0] == "🚨 ESCALATED"


def test_status_badge_mitigated_when_executed_but_not_converged():
    e = LedgerEntry(entry_id="1", incident_id="i", state=IncidentState.MONITORING, event_type="execution_success", source="p2")
    assert compute_status([e])[0].startswith("🟡 MITIGATED")


def test_status_badge_unresolved_when_nothing_executed():
    e = LedgerEntry(entry_id="1", incident_id="i", state=IncidentState.TRIAGE, event_type="alert_received", source="c")
    assert compute_status([e])[0] == "🔵 UNRESOLVED"


def test_timeline_narrative_mentions_key_milestones(entries):
    text = build_timeline_narrative(entries)
    assert "TC_MUTATE_Frontend_Traffic_f83b7416" in text
    assert "`scale_deployment` aborted by safety gate" in text
    assert "converged" in text


def test_hypothesis_evolution_has_original_revision_and_evidence(entries):
    text = build_hypothesis_evolution(entries)
    assert "Initial Hypothesis" in text and "Running" in text
    assert "Revision 1" in text and "Revision 2" in text
    assert "IMAGE_PULL_FAILURE" in text
    assert "ImagePullBackOff" in text
    assert "rollback_release" in text


def test_hypothesis_evolution_includes_operator_steering():
    steer = LedgerEntry(
        entry_id="1", incident_id="i", state=IncidentState.REMEDIATION, event_type="steer",
        source="carol", details={"action_id": "a1", "channel": "telegram", "instruction": "do not scale, check image tag"},
    )
    text = build_hypothesis_evolution([steer])
    assert "Operator Steering" in text and "carol" in text and "check image tag" in text


def test_hypothesis_evolution_without_replans_states_so():
    e = LedgerEntry(entry_id="1", incident_id="i", state=IncidentState.TRIAGE, event_type="alert_received", source="c")
    assert "No re-planning" in build_hypothesis_evolution([e])


def test_enforce_sections_overrides_llm_and_inserts_missing(entries):
    llm = "# Incident Report — i\n\n## Summary\n- x\n\n## Timeline\nLLM junk\n\n## Actions Taken\nonly last action\n\n## Recommendations\nr\n"
    out = enforce_sections(
        llm, status_badge="> **Incident Status: ✅ RESOLVED** — ok", timeline_block="TL", evolution_block="EVO", actions_block="ALL ACTIONS",
    )
    assert "LLM junk" not in out and "only last action" not in out
    assert out.index("Incident Status") < out.index("## Summary")
    assert out.index("## Timeline") < out.index("## Hypothesis Evolution & Operator Steering") < out.index("## Actions Taken")
    assert "ALL ACTIONS" in out and "## Recommendations" in out


def test_report_builder_renders_runbook_citations_from_ledger():
    cit = {
        "runbook_id": "feature-flag-failure",
        "book_name": "Feature Flag Failure Mitigation",
        "section": "Remediation Steps",
        "line_range": "L46-L49",
        "chunk_id": "chunk 4/5",
        "referenced_text": "Disable offending flag via toggle_feature_flag.",
    }
    proposed = LedgerEntry(
        entry_id="evt-cit-1",
        incident_id="inc-cit-rep",
        state=IncidentState.AWAITING_APPROVAL,
        event_type="action_proposed",
        source="phase2_worker",
        details={
            "action_id": "act-cit-1",
            "runbook_citations": [cit],
            "proposal": {
                "confidence": 0.94,
                "risk_assessment": "Low risk",
                "runbook_citations": [cit],
                "actions": [
                    {
                        "tool_name": "toggle_feature_flag",
                        "target_resource": "otel-demo/cartFailure",
                        "parameters": {"flag_name": "cartFailure", "enabled": False},
                        "rationale": (
                            "Disable cartFailure\n\n[Runbook Reference]\n"
                            "id/title: 'feature-flag-failure/Feature Flag Failure Mitigation'\n"
                            "section/line_range: 'Remediation Steps/L46-L49 (chunk 4/5)'"
                        ),
                        "runbook_citations": [cit],
                    }
                ],
            },
        },
    )
    timeline_row = humanize_details(proposed)
    assert "feature-flag-failure/Feature Flag Failure Mitigation" in timeline_row
    assert "Remediation Steps/L46-L49 (chunk 4/5)" in timeline_row
    assert "{" not in timeline_row and "}" not in timeline_row

    actions_md = build_actions_section([proposed])
    assert "- **Rationale:** Disable cartFailure\n" in actions_md
    assert "[Runbook Reference]" not in actions_md
    assert "**Runbook Citations:**" in actions_md
    assert "`id/title`: `'feature-flag-failure/Feature Flag Failure Mitigation'`" in actions_md
    assert "`section/line_range`: `'Remediation Steps/L46-L49 (chunk 4/5)'`" in actions_md
    assert '`reference`: *"Disable offending flag via toggle_feature_flag."*' in actions_md
    assert "&nbsp;" not in actions_md


def test_build_recovery_section_renders_observed_signals_and_pod_checks():
    from datetime import datetime, timezone

    conv_entry = LedgerEntry(
        entry_id="evt-conv-1",
        incident_id="inc-rec-1",
        timestamp=datetime(2026, 11, 22, 16, 15, 0, tzinfo=timezone.utc),
        state=IncidentState.REPORTING,
        event_type="signals_converged",
        source="phase2_worker",
        details={
            "convergence": {
                "converged": True,
                "monitored_services": ["frontend"],
                "workload_mode": "stateless",
                "bake_time_seconds": 30,
                "window_seconds": 60,
                "poll_interval_seconds": 20,
                "clean_samples": 3,
                "target_clean_samples": 3,
                "slo_thresholds": {
                    "error_rate_threshold": 0.01,
                    "p99_latency_ms": 500.0,
                    "cpu_saturation_threshold": 0.85,
                    "memory_saturation_threshold": 0.85,
                },
                "signals": {
                    "frontend.error_rate": 0.0,
                    "frontend.p99_latency_ms": 112.5,
                    "frontend.cpu_saturation": 0.184,
                    "frontend.memory_saturation": 0.412,
                },
                "pod_checks": [
                    {
                        "pod": "frontend-7d8f9c6b5-k2m9z",
                        "service": "frontend",
                        "phase": "Running",
                        "ready": True,
                        "healthy": True,
                    }
                ],
            }
        },
    )
    recovery_md = build_recovery_section([conv_entry])
    assert "*Confirmed* at `2026-11-22 16:15:00 UTC`" in recovery_md
    assert "`frontend` (*stateless*)" in recovery_md
    assert "bake time `30s`, `3/3` consecutive clean samples, `60s` SLO window (`20s` poll interval)" in recovery_md
    assert "| `frontend` | Error Rate (5xx) | `0.00%` | `<= 1.00%` | *PASS* |" in recovery_md
    assert "| `frontend` | P99 Latency | `112.5 ms` | `<= 500 ms` | *PASS* |" in recovery_md
    assert "| `frontend` | CPU Saturation | `18.4%` | `<= 85.0%` | *PASS* |" in recovery_md
    assert "| `frontend` | Memory Saturation | `41.2%` | `<= 85.0%` | *PASS* |" in recovery_md
    assert "`frontend-7d8f9c6b5-k2m9z` — *Running*, `Ready` (*PASS*)" in recovery_md


def test_build_recovery_section_timeout_shows_breached_signals_not_placeholder():
    from datetime import datetime, timezone

    nc_entry = LedgerEntry(
        entry_id="evt-nc-1",
        incident_id="inc-rec-2",
        timestamp=datetime(2026, 11, 22, 16, 20, 0, tzinfo=timezone.utc),
        state=IncidentState.REMEDIATION,
        event_type="signals_not_converged",
        source="phase2_worker",
        details={
            "prior_attempts_count": 1,
            "signals_at_timeout": {
                "cart.error_rate": 0.25,
                "cart.p99_latency_ms": 840.0,
            },
            "convergence": {
                "converged": False,
                "slo_thresholds": {
                    "error_rate_threshold": 0.01,
                    "p99_latency_ms": 500.0,
                },
                "signals": {
                    "cart.error_rate": 0.25,
                    "cart.p99_latency_ms": 840.0,
                },
            },
        },
    )
    recovery_md = build_recovery_section([nc_entry])
    assert "*Not Confirmed*" in recovery_md
    assert "Error rate < 1%" not in recovery_md
    assert "| `cart` | Error Rate (5xx) | `25.00%` | `<= 1.00%` | *BREACH* |" in recovery_md
    assert "| `cart` | P99 Latency | `840.0 ms` | `<= 500 ms` | *BREACH* |" in recovery_md

    enforced = enforce_sections(
        "# Incident Report — i\n\n## Summary\n- x\n\n## Timeline\nold\n\n## Hypothesis Evolution & Operator Steering\nold\n\n## Actions Taken\nold\n\n## Recovery Confirmation\nOld stub\n\n## Recommendations\nr\n",
        status_badge="> **STATUS: ESCALATED**",
        timeline_block="TL",
        evolution_block="EVO",
        actions_block="ACT",
        recovery_block=recovery_md,
    )
    assert "Old stub" not in enforced
    assert "25.00%" in enforced
    assert enforced.index("## Actions Taken") < enforced.index("## Recovery Confirmation") < enforced.index("## Recommendations")


def test_collect_operator_inputs_across_channels_and_events():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 11, 22, 16, 30, 0, tzinfo=timezone.utc)
    entries = [
        LedgerEntry(
            entry_id="op-0", incident_id="inc-op-1", timestamp=t0,
            state=IncidentState.ESCALATED, event_type="escalate", source="sag_approval_timer",
            details={"reason": "approval_timeout"},  # System timer (no channel) -> filtered out
        ),
        LedgerEntry(
            entry_id="op-1", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=10),
            state=IncidentState.EXECUTING, event_type="approved", source="alice",
            details={"channel": "telegram", "reason": "Verified pod hang in logs"},
        ),
        LedgerEntry(
            entry_id="op-2", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=20),
            state=IncidentState.STANDBY, event_type="pause", source="operator",
            details={"channel": "dashboard", "reason": "Holding for DB migration check"},
        ),
        LedgerEntry(
            entry_id="op-3", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=30),
            state=IncidentState.REMEDIATION, event_type="resume", source="operator",
            details={"channel": "dashboard", "instruction": "Continue monitoring after migration"},
        ),
        LedgerEntry(
            entry_id="op-4", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=40),
            state=IncidentState.REMEDIATION, event_type="steer", source="telegram:1:bob",
            details={"channel": "telegram", "instruction": "Scale replicas to 3 instead of restarting"},
        ),
        LedgerEntry(
            entry_id="op-5", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=50),
            state=IncidentState.ESCALATED, event_type="denied", source="bob",
            details={"channel": "dashboard", "reason": "Freeze window active"},
        ),
        LedgerEntry(
            entry_id="op-6", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=60),
            state=IncidentState.ESCALATED, event_type="escalate", source="telegram:1:bob",
            details={"channel": "telegram", "reason": "Paging primary on-call"},
        ),
        LedgerEntry(
            entry_id="op-7", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=70),
            state=IncidentState.REPORTING, event_type="generate_report", source="operator",
            details={"channel": "dashboard", "reason": "Closing with full report"},
        ),
        LedgerEntry(
            entry_id="op-8", incident_id="inc-op-1", timestamp=t0 + timedelta(seconds=80),
            state=IncidentState.CLOSED, event_type="drop", source="telegram:1:alice",
            details={"channel": "telegram", "reason": "Resolved manually by SRE"},
        ),
    ]

    collected = collect_operator_inputs(entries)
    assert [c["action"] for c in collected] == [
        "approved", "pause", "resume", "steer", "denied", "escalate", "generate_report", "drop"
    ]
    assert collected[0]["channel"] == "telegram"
    assert collected[1]["channel"] == "dashboard"

    evo_md = build_hypothesis_evolution(entries)
    assert "### Operator Inputs & Audit Notes" in evo_md
    assert "Verified pod hang in logs" in evo_md
    assert "Holding for DB migration check" in evo_md
    assert "Continue monitoring after migration" in evo_md
    assert "Scale replicas to 3 instead of restarting" in evo_md
    assert "Freeze window active" in evo_md
    assert "Paging primary on-call" in evo_md
    assert "Closing with full report" in evo_md
    assert "Resolved manually by SRE" in evo_md
    assert "approval_timeout" not in evo_md

    # Humanized timeline row shows channel + reason for close/pause
    pause_row = humanize_details(entries[2])
    assert "`dashboard`" in pause_row and "Holding for DB migration check" in pause_row

