import json
from pathlib import Path
from typing import Any, List, Optional
import redis

from shared.models.ledger import LedgerEntry, IncidentReport


def append_ledger_entry(
    redis_client: Optional[Any],
    incident_id: str,
    entry: LedgerEntry,
) -> None:
    key = f"incident:{incident_id}:ledger"
    entry_json = entry.model_dump_json()
    if redis_client is not None:
        res = redis_client.rpush(key, entry_json)
        import inspect
        if inspect.isawaitable(res):
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(res)
            except RuntimeError:
                pass


def get_ledger_entries(
    redis_client: Optional[redis.Redis],
    incident_id: str,
) -> List[LedgerEntry]:
    if redis_client is None:
        return []
    key = f"incident:{incident_id}:ledger"
    raw_entries = redis_client.lrange(key, 0, -1)
    entries: List[LedgerEntry] = []
    for raw in raw_entries:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            entries.append(LedgerEntry.model_validate_json(raw))
        except Exception as exc:
            logger.warning("Failed to validate ledger entry for incident %s: %s", incident_id, exc)
    return entries


def dump_ledger_to_disk(
    storage_path: str,
    incident_id: str,
    entries: List[LedgerEntry],
    report: Optional[IncidentReport] = None,
    topology_snapshot: Optional[dict] = None,
) -> Path:
    target_path = Path(storage_path)
    # If path starts with /data but /data doesn't exist on host, fallback to ./data
    if str(target_path).startswith(("/data", "\\data")) and not Path("/data").exists():
        rel = str(target_path).lstrip("/\\").removeprefix("data").lstrip("/\\")
        target_path = (Path.cwd() / "data" / rel).resolve()

    inc_dir = target_path / incident_id
    inc_dir.mkdir(parents=True, exist_ok=True)

    # 1. Full ledger JSON dump
    ledger_file = inc_dir / "ledger_full.json"
    with open(ledger_file, "w", encoding="utf-8") as f:
        json.dump([e.model_dump(mode="json") for e in entries], f, indent=2)

    # 2. Report Markdown dump
    if report:
        report_file = inc_dir / "report.md"
        with open(report_file, "w", encoding="utf-8") as f:
            if getattr(report, "markdown_content", None):
                f.write(report.markdown_content.strip() + "\n")
            else:
                f.write(f"# Incident Report — {report.title}\n\n")
                f.write(f"- **Incident ID:** {report.incident_id}\n")
                f.write(f"- **Severity:** {report.severity}\n")
                f.write(f"- **Service:** {report.affected_service} ({report.affected_namespace})\n")
                f.write(f"- **Duration:** {report.duration_seconds:.1f}s\n\n")
                f.write(f"## Root Cause Summary\n{report.root_cause_summary}\n\n")
                f.write(f"## Remediation Summary\n{report.remediation_summary}\n\n")
                if report.timeline:
                    f.write("## Timeline\n\n")
                    f.write("| Time | State | Event | Source |\n")
                    f.write("|------|-------|-------|--------|\n")
                    for entry in report.timeline:
                        ts = entry.timestamp.strftime("%H:%M:%S")
                        f.write(f"| {ts} | {entry.state.value} | {entry.event_type} | {entry.source} |\n")
                    f.write("\n")
                if report.action_log:
                    f.write("## Actions Taken\n\n")
                    for action in report.action_log:
                        tool = action.get("tool_name", "unknown")
                        target = action.get("target_resource", "")
                        outcome = action.get("outcome", "")
                        approver = action.get("approver", "")
                        channel = action.get("channel", "")
                        f.write(f"- `{tool}` on `{target}` — approver: {approver} ({channel}) — {outcome}\n")
                    f.write("\n")
                if report.preventative_recommendations:
                    f.write("## Recommendations\n")
                    for rec in report.preventative_recommendations:
                        f.write(f"- {rec}\n")

    # 3. Topology snapshot
    if topology_snapshot:
        topo_file = inc_dir / "topology_snapshot.json"
        with open(topo_file, "w", encoding="utf-8") as f:
            json.dump(topology_snapshot, f, indent=2)

    return inc_dir
