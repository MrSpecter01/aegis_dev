import pytest
from datetime import datetime, timezone

from shared.models import LedgerEntry, IncidentReport, IncidentState
from shared.ledger import dump_ledger_to_disk


def test_dump_ledger_to_disk(tmp_path):
    storage_dir = str(tmp_path / "incidents")
    incident_id = "inc-20260830-test"

    entry = LedgerEntry(
        entry_id="led-001",
        incident_id=incident_id,
        state=IncidentState.NEW,
        event_type="alert_received",
        source="alertmanager",
    )
    report = IncidentReport(
        incident_id=incident_id,
        title="Test Incident Report",
        severity="P2",
        affected_service="cartservice",
        affected_namespace="boutique",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
        duration_seconds=60.0,
        root_cause_summary="Memory leak in cartservice container",
        remediation_summary="Restarted pod via SAG",
        preventative_recommendations=["Increase memory limit to 512Mi"],
    )

    out_dir = dump_ledger_to_disk(
        storage_path=storage_dir,
        incident_id=incident_id,
        entries=[entry],
        report=report,
        topology_snapshot={"nodes": []},
    )

    assert (out_dir / "ledger_full.json").exists()
    assert (out_dir / "report.md").exists()
    assert (out_dir / "topology_snapshot.json").exists()
