"""Deterministic, ledger-grounded section builders for Scribe incident reports.

Everything here is derived strictly from ledger entries (and optionally the Phase 1
context). Nothing is invented: when a field is absent the output says so. These
sections are rendered in Python so they are complete (every action, every re-plan)
and are enforced onto the LLM output regardless of what the model wrote.
"""

from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from shared.models.ledger import LedgerEntry

GAP_THRESHOLD_SECONDS = 300.0
_DETAIL_MAX_CHARS = 240

TIMELINE_HEADER = "| Time (UTC) | Event | State | Source | Details |"
TIMELINE_DIVIDER = "|---|---|---|---|---|"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _ev(entry: LedgerEntry) -> str:
    return entry.event_type or ""


def _state(entry: LedgerEntry) -> str:
    return entry.state.value if hasattr(entry.state, "value") else str(entry.state)


def _hms(ts: datetime) -> str:
    return ts.strftime("%H:%M:%S")


def _truncate(text: str, limit: int = _DETAIL_MAX_CHARS) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]) + "…"


def _cell(text: str) -> str:
    """Make text safe for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _flatten(value: Any) -> str:
    """Render arbitrary JSON-ish data as plain `key: value; key: value` text (no braces)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, dict):
        parts = [f"{k}: {_flatten(v)}" for k, v in value.items() if v not in (None, "", [], {})]
        return "; ".join(parts) if parts else "—"
    if isinstance(value, (list, tuple)):
        parts = [_flatten(v) for v in value if v not in (None, "", [], {})]
        return ", ".join(parts) if parts else "—"
    return str(value)


def _params_text(params: Any) -> str:
    if not isinstance(params, dict) or not params:
        return "no parameters"
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _actions_of(entry: LedgerEntry) -> List[Dict[str, Any]]:
    proposal = (entry.details or {}).get("proposal") or {}
    actions = proposal.get("actions") or []
    return [a for a in actions if isinstance(a, dict)]


def _exec_message(result_item: Dict[str, Any]) -> str:
    """Pull the human message out of an MCP tool result when present."""
    result = result_item.get("result") or {}
    structured = (result.get("structuredContent") or {}).get("result") or {}
    if isinstance(structured, dict) and structured.get("message"):
        return str(structured["message"])
    if result_item.get("error"):
        return str(result_item["error"])
    return ""


# --------------------------------------------------------------------------- #
# Details (humanised) and one-liner ("what this means")
# --------------------------------------------------------------------------- #

def humanize_details(entry: LedgerEntry) -> str:
    """Convert a ledger entry's details dict into a clean string with italicized indicators."""
    d = entry.details or {}
    ev = _ev(entry)
    if not d:
        return "—"

    if ev == "alert_received":
        grp = d.get("alert_group") or {}
        prim = grp.get("primary_alert") or {}
        labels = prim.get("labels") or {}
        summary = (prim.get("annotations") or {}).get("summary", "")
        bits = [f"alert `{labels.get('alertname', 'unknown')}`"]
        if summary:
            bits.append(summary)
        if grp.get("affected_service"):
            bits.append(f"service `{grp['affected_service']}` (*{grp.get('affected_namespace', '?')}*)")
        return _truncate("; ".join(bits))
    if ev == "grouping_complete":
        return f"incident `{d.get('incident_id', '')}` created"
    if ev == "merge_complete":
        hyp = d.get("primary_hypothesis", "")
        hyp_clean = re.sub(r"'(frontend-[a-z0-9-]+)'", r"`\1`", hyp)
        hyp_clean = re.sub(r"\b(Running|ImagePullBackOff|CrashLoopBackOff)\b", r"*\1*", hyp_clean)
        return _truncate(
            f"confidence *{d.get('merged_confidence', '?')}*, corroboration *{d.get('corroboration_score', '?')}*: {hyp_clean}"
        )
    if ev == "action_proposed":
        acts = _actions_of(entry)
        prop = d.get("proposal") or {}
        if acts:
            desc = "; ".join(f"`{a.get('tool_name')}` on `{a.get('target_resource')}` ({_params_text(a.get('parameters'))})" for a in acts)
            cits = prop.get("runbook_citations") or d.get("runbook_citations") or acts[0].get("runbook_citations") or []
            cit_note = ""
            if isinstance(cits, list) and cits and isinstance(cits[0], dict):
                c0 = cits[0]
                b = str(c0.get("book_name") or c0.get("runbook_id") or "").replace("{", "(").replace("}", ")")
                r_id = str(c0.get("runbook_id") or "").replace("{", "(").replace("}", ")")
                id_title = f"{r_id}/{b}" if (r_id and b and r_id.lower() != b.lower()) else (r_id or b)
                s = str(c0.get("section") or "General").replace("{", "(").replace("}", ")")
                l_rng = str(c0.get("line_range") or "").replace("{", "(").replace("}", ")")
                c_id = str(c0.get("chunk_id") or "").replace("{", "(").replace("}", ")")
                sec_loc = f"{s}/{l_rng}" if l_rng else s
                if c_id:
                    sec_loc += f" ({c_id})"
                if id_title:
                    cit_note = f"; cited '{id_title}' [{sec_loc}]"
            return _truncate(f"{desc}; confidence *{prop.get('confidence', '?')}*{cit_note}")
    if ev in ("approved", "denied"):
        reason = d.get("reason")
        base = f"via `{d.get('channel', '?')}`"
        return _truncate(f"{base}; reason: *{reason}*" if reason else base)
    if ev == "steer":
        return _truncate(f"instruction: *\"{d.get('instruction', '—')}\"* (via `{d.get('channel', '?')}`)")
    if ev in ("pause", "resume", "resume_executing", "resume_monitoring", "resume_approval", "resume_triage", "escalate", "escalation", "generate_report", "force_close", "drop"):
        ch = d.get("channel")
        base = f"via `{ch}`" if ch else ""
        op_input = d.get("reason") or d.get("instruction")
        if op_input and base:
            return _truncate(f"{base}; reason: *{op_input}*")
        if op_input:
            return _truncate(f"reason: *{op_input}*")
        if base:
            return base
    if ev == "pre_execution_diagnostics":
        verdict = f"*{d.get('gate_verdict')}*"
        f_class = f"*{d.get('failure_class')}*"
        return _truncate(f"`{d.get('tool_name')}`: {verdict} — {f_class}. {d.get('reason', '')}")
    if ev in ("pre_execution_mutation_detected", "pre_exec_replan"):
        cls = f"*{d.get('failure_class') or d.get('new_failure_class')}*"
        return _truncate(f"aborted `{d.get('aborted_tool')}`; live fault {cls}. {d.get('reason', '')}")
    if ev == "execution_success":
        results = d.get("execution_results") or []
        parts = []
        for r in results:
            if isinstance(r, dict):
                msg = _exec_message(r)
                status_str = f"*{r.get('status')}*"
                parts.append(f"`{r.get('tool')}`: {status_str}" + (f" — {msg}" if msg else ""))
        if parts:
            return _truncate("; ".join(parts))
    if ev == "signals_not_converged":
        return f"attempt *{d.get('prior_attempts_count', '?')}* did not restore signals"
    if ev == "signals_converged":
        return "all monitored signals back within bounds" if (d.get("convergence") or {}).get("converged") else _truncate(_flatten(d))

    return _truncate(_flatten(d))


def event_one_liner(entry: LedgerEntry) -> str:
    """One plain-language sentence explaining what this ledger row means."""
    d = entry.details or {}
    ev = _ev(entry)
    one_liners = {
        "alert_received": "A monitoring alert fired and Aegis started an incident.",
        "grouping_complete": "Related alerts were grouped into one incident; investigation begins.",
        "triage_complete": "The investigator agents finished their analysis.",
        "merge_complete": "Investigator findings were merged into a single leading hypothesis.",
        "action_proposed": "Aegis proposed a fix and is waiting for an operator to approve it.",
        "approved": f"{entry.source} approved the proposed fix, so it may run.",
        "denied": f"{entry.source} rejected the proposed fix; it will not run.",
        "steer": f"{entry.source} gave new guidance and asked for the plan to be redone.",
        "pre_execution_diagnostics": "Safety check of live cluster state just before running the approved fix.",
        "pre_execution_mutation_detected": "Live state no longer matched the plan, so the fix was cancelled before running.",
        "pre_exec_replan": "The remediation agent is re-planning based on the newly detected fault.",
        "execution_success": "The approved fix ran successfully; recovery is now being monitored.",
        "execution_failed": "The approved fix failed to run; the incident was escalated.",
        "signals_not_converged": "The fix did not bring health signals back to normal; another attempt is needed.",
        "signals_converged": "Health signals returned to normal; the incident is considered recovered.",
        "escalation": "The incident was handed to a human operator.",
        "report_delivered": "This report was saved and delivered.",
    }
    line = one_liners.get(ev)
    if ev == "pre_execution_diagnostics" and d.get("gate_verdict") == "FAULT_CONFIRMED_PROCEED":
        line = "Live check confirmed the fault, so the approved fix is safe to run."
    if ev == "pre_execution_diagnostics" and d.get("gate_verdict") == "MUTATION_DETECTED_REPLAN":
        line = "Live check found a different fault than planned for; the fix will be cancelled."
    return line or f"Recorded '{ev.replace('_', ' ')}' event."


# --------------------------------------------------------------------------- #
# Timeline table + gaps
# --------------------------------------------------------------------------- #

def build_timeline_table(entries: List[LedgerEntry]) -> Tuple[str, List[str]]:
    """Render the ledger as a table: Time → Event → State → Source → Details."""
    if not entries:
        row = "| [no record] | [UNKNOWN — no ledger entry] | [no record] | system | No entries recorded |"
        return "\n".join([TIMELINE_HEADER, TIMELINE_DIVIDER, row]), ["No ledger entries found"]

    rows = [TIMELINE_HEADER, TIMELINE_DIVIDER]
    ordered = sorted(entries, key=lambda e: e.timestamp)
    gaps: List[str] = []
    for i, entry in enumerate(ordered):
        if i > 0:
            diff = (entry.timestamp - ordered[i - 1].timestamp).total_seconds()
            if diff > GAP_THRESHOLD_SECONDS:
                gap_msg = f"[UNKNOWN — no ledger entry: {diff:.0f}s gap]"
                gaps.append(gap_msg)
                rows.append(
                    f"| {_hms(ordered[i - 1].timestamp)} | {gap_msg} | GAP | monitor | Unmonitored time gap |"
                )
        rows.append(
            f"| {_hms(entry.timestamp)} | {_cell(_ev(entry))} | {_state(entry)} | {_cell(entry.source)} "
            f"| {_cell(humanize_details(entry))} |"
        )
    return "\n".join(rows), gaps


# --------------------------------------------------------------------------- #
# Status badge
# --------------------------------------------------------------------------- #

def compute_status(entries: List[LedgerEntry]) -> Tuple[str, str]:
    """Return (badge, explanation) describing incident status at report generation time."""
    ordered = sorted(entries, key=lambda e: e.timestamp)
    events = [_ev(e) for e in ordered]
    replans = sum(1 for e in events if e in ("pre_exec_replan", "signals_not_converged", "steer"))
    executed = sum(1 for e in events if e == "execution_success")
    suffix = f" Plan was revised {replans} time(s)." if replans else ""

    last_exec = max((i for i, e in enumerate(events) if e == "execution_success"), default=-1)
    conv = [i for i, e in enumerate(events) if e == "signals_converged"]
    if conv and conv[-1] > last_exec:
        return "✅ RESOLVED", f"Health signals converged at {_hms(ordered[conv[-1]].timestamp)} UTC after {executed} executed fix(es).{suffix}"

    escalated = any(e in ("denied", "escalation", "escalate", "execution_failed", "retry_limit_reached") for e in events) or (
        ordered and _state(ordered[-1]) == "ESCALATED"
    )
    if escalated:
        triggers = []
        if any(e == "denied" for e in events):
            triggers.append("operator denial")
        if any(e == "execution_failed" for e in events):
            triggers.append("failed execution")
        if any(e == "retry_limit_reached" for e in events):
            triggers.append("retry limit reached")
        detail = f" ({', '.join(triggers)})" if triggers else ""
        return "🚨 ESCALATED", f"Automated remediation stopped{detail}; escalated for human intervention.{suffix}"
    if executed:
        return "🟡 MITIGATED — RECOVERY NOT CONFIRMED", f"A fix was applied but health signals were not confirmed healthy.{suffix}"
    return "🔵 UNRESOLVED", f"No remediation was applied before this report was generated.{suffix}"


def build_status_badge(entries: List[LedgerEntry]) -> str:
    badge, text = compute_status(entries)
    return f"> **STATUS: {badge}**  \n> <sub>{text}</sub>"


# --------------------------------------------------------------------------- #
# Natural-language timeline summary
# --------------------------------------------------------------------------- #

def build_timeline_narrative(entries: List[LedgerEntry]) -> str:
    """Ultra-concise chronological summary of key inflection points (no redundant timestamps)."""
    if not entries:
        return "[UNKNOWN — no ledger entry]"
    ordered = sorted(entries, key=lambda e: e.timestamp)
    t0 = ordered[0].timestamp
    steps: List[str] = []

    for e in ordered:
        ev, d = _ev(e), e.details or {}
        if ev == "alert_received":
            grp = d.get("alert_group") or {}
            prim = grp.get("primary_alert") or {}
            name = (prim.get("labels") or {}).get("alertname", "")
            svc = (prim.get("labels") or {}).get("service") or grp.get("affected_service", "service")
            summary = (prim.get("annotations") or {}).get("summary")
            alert_ref = f"`{name}`" if name else "alert"
            desc = f" ({summary})" if summary else ""
            steps.append(f"Alert {alert_ref} fired on `{svc}`{desc}")
        elif ev == "merge_complete":
            steps.append("Triage suspected traffic demand")
        elif ev == "execution_success":
            tools = [r.get("tool") for r in (d.get("execution_results") or []) if isinstance(r, dict)]
            tool_str = ", ".join(f"`{t}`" for t in tools) if tools else "remediation"
            steps.append(f"{tool_str} executed")
        elif ev == "signals_not_converged":
            steps.append("Signals unconverged; re-planning")
        elif ev == "pre_execution_mutation_detected":
            f_class = d.get("failure_class", "mutation")
            aborted = d.get("aborted_tool", "action")
            steps.append(f"`{aborted}` aborted by safety gate (*{f_class}* detected); re-planning")
        elif ev == "steer":
            inst = d.get("instruction", "")
            inst_short = (inst[:35] + "…") if len(inst) > 38 else inst
            steps.append(f"Operator steered: *\"{inst_short}\"*")
        elif ev == "denied":
            steps.append(f"Action denied by `{e.source}`; escalated")
        elif ev == "signals_converged":
            duration = (e.timestamp - t0).total_seconds()
            steps.append(f"Signals converged ({duration:.0f}s total)")
        elif ev == "execution_failed":
            steps.append("Execution failed; escalated")
        elif ev == "escalation":
            steps.append("Escalated to human operator")

    if not steps:
        return "[UNKNOWN — no ledger entry]"

    return " -> ".join(steps)


# --------------------------------------------------------------------------- #
# Actions Taken — every action, including aborted / replanned / denied
# --------------------------------------------------------------------------- #

def collect_actions(entries: List[LedgerEntry]) -> List[Dict[str, Any]]:
    """Walk the ledger and return one record per proposed action with its full lifecycle."""
    ordered = sorted(entries, key=lambda e: e.timestamp)
    records: List[Dict[str, Any]] = []
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    current_id: Optional[str] = None

    for e in ordered:
        ev, d = _ev(e), e.details or {}
        if ev == "action_proposed":
            prop = d.get("proposal") or {}
            aid = d.get("action_id") or f"unknown-{len(records)}"
            group: List[Dict[str, Any]] = []
            for a in _actions_of(e):
                rec = {
                    "action_id": aid,
                    "proposed_at": e.timestamp,
                    "tool": a.get("tool_name", "unknown"),
                    "target": a.get("target_resource", "unknown"),
                    "params": a.get("parameters") or {},
                    "rationale": a.get("rationale") or prop.get("rationale") or "[no record]",
                    "runbook_citations": a.get("runbook_citations") or prop.get("runbook_citations") or d.get("runbook_citations") or [],
                    "confidence": prop.get("confidence"),
                    "risk": prop.get("risk_assessment"),
                    "decision": "pending",
                    "decided_by": None,
                    "channel": None,
                    "decision_reason": None,
                    "outcome": "proposed — no execution recorded",
                }
                group.append(rec)
                records.append(rec)
            by_id[aid] = group
            current_id = aid
        elif ev in ("approved", "denied"):
            aid = d.get("action_id") or current_id
            for rec in by_id.get(aid or "", []):
                rec["decision"] = ev
                rec["decided_by"] = e.source
                rec["channel"] = d.get("channel")
                rec["decision_reason"] = d.get("reason")
                rec["outcome"] = "approved — execution not recorded" if ev == "approved" else "denied — not executed"
        elif ev == "steer":
            aid = d.get("action_id") or current_id
            for rec in by_id.get(aid or "", []):
                rec["decision"] = "steered"
                rec["decided_by"] = e.source
                rec["channel"] = d.get("channel")
                rec["decision_reason"] = d.get("instruction")
                rec["outcome"] = "superseded by operator steering — not executed"
        elif ev == "pre_execution_mutation_detected":
            tool = d.get("aborted_tool")
            for rec in reversed(by_id.get(current_id or "", [])):
                if rec["tool"] == tool:
                    rec["outcome"] = f"aborted before execution by safety check (live fault {d.get('failure_class')}): {_truncate(d.get('reason', ''), 200)}"
                    break
        elif ev in ("execution_success", "execution_failed"):
            status_default = "success" if ev == "execution_success" else "failed"
            for r in d.get("execution_results") or []:
                if not isinstance(r, dict):
                    continue
                aid = None
                structured = ((r.get("result") or {}).get("structuredContent") or {}).get("result") or {}
                if isinstance(structured, dict):
                    aid = structured.get("action_id")
                candidates = by_id.get(aid or current_id or "", [])
                for rec in reversed(candidates):
                    if rec["tool"] == r.get("tool"):
                        msg = _exec_message(r)
                        status = r.get("status", status_default)
                        rec["outcome"] = f"executed — {status}" + (f": {msg}" if msg else "") + f" (at {_hms(e.timestamp)})"
                        break
    return records


def build_actions_section(entries: List[LedgerEntry]) -> str:
    """Render EVERY action attempted, in order, with spaced-out technical indicators."""
    records = collect_actions(entries)
    if not records:
        return "[no record] — no remediation actions were proposed in the ledger."

    lines: List[str] = []
    for i, rec in enumerate(records, 1):
        decision = {
            "approved": f"approved by `{rec['decided_by']}` via `{rec['channel']}`",
            "denied": f"denied by `{rec['decided_by']}` via `{rec['channel']}`",
            "steered": f"superseded by steering from `{rec['decided_by']}`",
            "pending": "no approval decision recorded",
        }[rec["decision"]]

        # Format outcome with italics on status/indicators
        outcome_text = str(rec["outcome"])
        outcome_text = re.sub(r"\b(IMAGE_PULL_FAILURE|DEGRADED_LOAD|ImagePullBackOff)\b", r"*\1*", outcome_text)
        outcome_text = re.sub(r"\b(success|completed|failed|aborted)\b", r"*\1*", outcome_text)

        lines.append(f"### {i}. `{rec['tool']}` on `{rec['target']}`")
        lines.append(f"- **Execution Outcome:** {outcome_text}")
        lines.append(f"- **Parameters:** `{_params_text(rec['params'])}`")
        lines.append(f"- **Authorization:** {decision}")
        clean_rat = str(rec['rationale']).split("\n\n[Runbook Reference]")[0].split("[Runbook Ref:")[0].strip()
        lines.append(f"- **Rationale:** {clean_rat}")
        cits = rec.get("runbook_citations") or []
        if isinstance(cits, list) and cits:
            lines.append("- **Runbook Citations:**")
            for cit in cits:
                if isinstance(cit, dict):
                    b_name = str(cit.get("book_name") or cit.get("runbook_id") or "Runbook").strip()
                    r_id = str(cit.get("runbook_id") or "").strip()
                    id_title = (
                        f"{r_id}/{b_name}"
                        if (r_id and b_name and r_id.lower() != b_name.lower())
                        else (r_id or b_name)
                    )
                    sec = str(cit.get("section") or "General").strip()
                    l_rng = str(cit.get("line_range") or "—").strip()
                    c_id = str(cit.get("chunk_id") or "").strip()
                    sec_loc = f"{sec}/{l_rng}" + (f" ({c_id})" if c_id else "")
                    excerpt = str(cit.get("referenced_text") or "").strip()
                    lines.append(f"  - `id/title`: `'{id_title}'`")
                    lines.append(f"  - `section/line_range`: `'{sec_loc}'`")
                    if excerpt:
                        lines.append(f'  - `reference`: *"{_truncate(excerpt, 220)}"*')
                else:
                    lines.append(f"  - {cit}")
        if rec["confidence"] is not None:
            lines.append(f"- **Risk & Confidence:** confidence *{rec['confidence']}* | risk *{rec['risk'] or '[no record]'}*")
        if rec["decision_reason"]:
            lines.append(f"- **Operator Note:** *{rec['decision_reason']}*")
        lines.append("")

    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# Hypothesis evolution & operator steering
# --------------------------------------------------------------------------- #

def _agent_evidence_lines(context: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    fba = context.get("findings_by_agent")
    if not isinstance(fba, dict):
        return lines
    for role, f in fba.items():
        if not isinstance(f, dict):
            continue
        role_name = getattr(role, "value", str(role))
        lines.append(f"  - `{role_name}` (confidence {f.get('confidence', '?')}): {_truncate(f.get('hypothesis', ''), 180)}")
        for ev in (f.get("evidence") or [])[:2]:
            if isinstance(ev, dict):
                lines.append(f"    - evidence `{ev.get('source_tool')}`: {_truncate(ev.get('raw_output', ''), 110)}")
    return lines


_OPERATOR_ACTION_EVENTS = {
    "approved",
    "denied",
    "steer",
    "pause",
    "resume",
    "resume_executing",
    "resume_monitoring",
    "resume_approval",
    "resume_triage",
    "escalate",
    "generate_report",
    "force_close",
    "drop",
}
_SYSTEM_SOURCES = {
    "system",
    "sag_approval_timer",
    "startup_sweep",
    "phase1_worker",
    "phase2_worker",
    "phase3_worker",
    "scribe",
    "alertmanager",
    "convergence_monitor",
}


def collect_operator_inputs(entries: List[LedgerEntry]) -> List[Dict[str, str]]:
    """Collect every human operator reason or instruction recorded across all channels."""
    ordered = sorted(entries, key=lambda e: e.timestamp)
    records: List[Dict[str, str]] = []
    for entry in ordered:
        ev = _ev(entry)
        if ev not in _OPERATOR_ACTION_EVENTS:
            continue
        d = entry.details or {}
        if not isinstance(d, dict):
            continue
        raw_input = d.get("reason") or d.get("instruction") or ""
        clean_input = " ".join(str(raw_input).split()).strip()
        if not clean_input:
            continue
        channel = str(d.get("channel") or "").strip()
        src = str(entry.source or "operator").strip()
        if src in _SYSTEM_SOURCES and not channel:
            continue
        records.append({
            "time": _hms(entry.timestamp),
            "action": ev,
            "actor": src,
            "channel": channel or "api",
            "input": clean_input,
        })
    return records


def build_hypothesis_evolution(entries: List[LedgerEntry], context: Optional[Dict[str, Any]] = None) -> str:
    """Original hypothesis, every revision (with reasoning and evidence), and operator steering."""
    context = context or {}
    ordered = sorted(entries, key=lambda e: e.timestamp)
    out: List[str] = []

    merge = next((e for e in ordered if _ev(e) == "merge_complete"), None)
    if merge:
        d = merge.details or {}
        hyp = d.get('primary_hypothesis', '[no record]')
        hyp_fmt = re.sub(r"'(frontend-[a-z0-9-]+)'", r"`\1`", hyp)
        hyp_fmt = re.sub(r"\b(Running|ImagePullBackOff|CrashLoopBackOff)\b", r"*\1*", hyp_fmt)
        out.append(f"### Initial Hypothesis *(at {_hms(merge.timestamp)} UTC)*")
        out.append(f"> {hyp_fmt}  ")
        out.append(f"> Confidence: *{d.get('merged_confidence', '?')}* | Corroboration: *{d.get('corroboration_score', '?')}*\n")
        agent_lines = _agent_evidence_lines(context)
        if agent_lines:
            out.append("- **Agent Inquiries & Leads:**")
            out.extend(agent_lines)
            out.append("")
    else:
        out.append("### Initial Hypothesis: *[no record]*\n")

    revision = 0
    for idx, e in enumerate(ordered):
        ev, d = _ev(e), e.details or {}
        if ev not in ("pre_exec_replan", "signals_not_converged", "steer"):
            continue
        revision += 1
        nxt = next((x for x in ordered[idx + 1:] if _ev(x) == "action_proposed"), None)
        revised_reason = ""
        if nxt:
            acts = _actions_of(nxt)
            if acts:
                revised_reason = f"`{acts[0].get('tool_name')}`: {acts[0].get('rationale', '')}"

        if ev == "pre_exec_replan":
            diag = next(
                (x for x in reversed(ordered[: idx + 1]) if _ev(x) == "pre_execution_diagnostics"), None
            )
            issues = ((diag.details or {}).get("pod_diagnostics_summary") or {}).get("critical_issues") if diag else None
            f_class = d.get('new_failure_class', 'unknown')
            aborted = d.get('aborted_tool', 'action')
            out.append(f"### Revision {revision} — Cause Shift Detected *(at {_hms(e.timestamp)} UTC)*")
            out.append(f"- **Safety Gate Verdict:** Aborted `{aborted}` — live diagnostics detected *{f_class}*.")
            out.append(f"- **Diagnostic Explanation:** {d.get('reason', '')}")
            if issues:
                iss_str = "; ".join(sorted(set(map(str, issues))))
                iss_str = re.sub(r"\b(ImagePullBackOff|CrashLoopBackOff|OOMKilled)\b", r"*\1*", iss_str)
                out.append(f"- **Evidence on Live Pods:** {iss_str}")
        elif ev == "signals_not_converged":
            out.append(f"### Revision {revision} — Fix Insufficient *(at {_hms(e.timestamp)} UTC)*")
            out.append(f"- **Observation:** Attempt *{d.get('prior_attempts_count', '?')}* did not restore golden health signals.")
        else:
            out.append(f"### Operator Steering *(at {_hms(e.timestamp)} UTC)*")
            out.append(f"- **Director:** `{e.source}` via `{d.get('channel', '?')}`")
            out.append(f"- **Instruction:** *\"{d.get('instruction', '[no record]')}\"*")

        if revised_reason:
            out.append(f"- **Course Correction:** {revised_reason}\n")
        else:
            out.append("")

    denials = [e for e in ordered if _ev(e) == "denied"]
    for e in denials:
        d = e.details or {}
        out.append(f"### Operator Denial *(at {_hms(e.timestamp)} UTC)*")
        out.append(f"- **Decided By:** `{e.source}` via `{d.get('channel', '?')}`")
        out.append(f"- **Reason:** *{d.get('reason', '[no reason recorded]')}*\n")

    if revision == 0 and not denials:
        out.append("\nNo re-planning or steering occurred; initial hypothesis remained valid throughout.")

    op_inputs = collect_operator_inputs(ordered)
    if op_inputs:
        out.append("\n### Operator Inputs & Audit Notes")
        out.append("| Time (UTC) | Action | Operator | Channel | Input / Reason |")
        out.append("|---|---|---|---|---|")
        for item in op_inputs:
            out.append(
                f"| `{item['time']}` | `{_cell(item['action'])}` | `{_cell(item['actor'])}` "
                f"| `{_cell(item['channel'])}` | {_cell(item['input'])} |"
            )

    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# Recovery Confirmation — grounded convergence telemetry & tier checks
# --------------------------------------------------------------------------- #

_SIGNAL_DISPLAY = {
    "error_rate": ("Error Rate (5xx)", "error_rate_threshold", "pct"),
    "p99_latency_ms": ("P99 Latency", "p99_latency_ms", "ms"),
    "cpu_saturation": ("CPU Saturation", "cpu_saturation_threshold", "pct"),
    "memory_saturation": ("Memory Saturation", "memory_saturation_threshold", "pct"),
}


def _format_signal_metric(metric_name: str, val: Any, thresholds: Dict[str, Any]) -> Tuple[str, str, str]:
    """Return (label, formatted_value_and_threshold, status) for a convergence metric."""
    label, thresh_key, unit = _SIGNAL_DISPLAY.get(metric_name, (metric_name, "", "raw"))
    thresh = thresholds.get(thresh_key) if thresh_key else None

    try:
        num_val = float(val)
    except (TypeError, ValueError):
        return label, str(val), "OBSERVED"

    try:
        num_thresh = float(thresh) if thresh is not None else None
    except (TypeError, ValueError):
        num_thresh = None

    if unit == "pct":
        val_str = f"`{num_val * 100:.2f}%`" if metric_name == "error_rate" else f"`{num_val * 100:.1f}%`"
        thresh_str = (
            f"`<= {num_thresh * 100:.2f}%`"
            if num_thresh is not None and metric_name == "error_rate"
            else (f"`<= {num_thresh * 100:.1f}%`" if num_thresh is not None else "—")
        )
    elif unit == "ms":
        val_str = f"`{num_val:.1f} ms`"
        thresh_str = f"`<= {num_thresh:.0f} ms`" if num_thresh is not None else "—"
    else:
        val_str = f"`{num_val}`"
        thresh_str = f"`<= {num_thresh}`" if num_thresh is not None else "—"

    if num_thresh is not None:
        status = "*PASS*" if num_val <= num_thresh else "*BREACH*"
    else:
        status = "OBSERVED"
    return f"{label}|{thresh_str}", val_str, status


def _render_signals_and_pods(
    signals: Dict[str, Any],
    thresholds: Dict[str, Any],
    pod_checks: List[Dict[str, Any]],
) -> List[str]:
    """Build Markdown table for golden signals and bullets for pod readiness checks."""
    out: List[str] = []
    metric_rows: List[str] = []
    inferred_pods: List[Dict[str, Any]] = []

    for key, val in signals.items():
        if key == "_meta":
            continue
        if key.endswith(".ready"):
            pod_name = key[: -len(".ready")]
            inferred_pods.append({
                "pod": pod_name,
                "ready": bool(val),
                "phase": "Running" if val else "NotReady",
                "healthy": bool(val),
            })
            continue
        if "." in key:
            svc, metric_name = key.rsplit(".", 1)
        else:
            svc, metric_name = "service", key
        label_and_thresh, val_str, status = _format_signal_metric(metric_name, val, thresholds)
        label, thresh_str = label_and_thresh.split("|", 1)
        metric_rows.append(f"| `{_cell(svc)}` | {_cell(label)} | {val_str} | {thresh_str} | {status} |")

    if metric_rows:
        out.append("| Service | Signal | Observed Value | SLO Threshold | Status |")
        out.append("|---|---|---|---|---|")
        out.extend(metric_rows)

    effective_pods = pod_checks or inferred_pods
    if effective_pods:
        out.append("")
        out.append("- **Kubernetes Pod Readiness Checks:**")
        seen_pods: set = set()
        for pc in effective_pods:
            if not isinstance(pc, dict):
                continue
            p_name = str(pc.get("pod") or "unknown")
            if p_name in seen_pods:
                continue
            seen_pods.add(p_name)
            p_phase = pc.get("phase", "Unknown")
            p_ready = "Ready" if pc.get("ready") else "Not Ready"
            p_health = "*PASS*" if pc.get("healthy", pc.get("ready")) else "*FAIL*"
            out.append(f"  - `{p_name}` — *{p_phase}*, `{p_ready}` ({p_health})")

    return out


def build_recovery_section(entries: List[LedgerEntry], context: Optional[Dict[str, Any]] = None) -> str:
    """Render grounded Recovery Confirmation section strictly from ledger and convergence telemetry."""
    context = context or {}
    ordered = sorted(entries, key=lambda e: e.timestamp)
    conv_entries = [e for e in ordered if _ev(e) == "signals_converged"]
    not_conv_entries = [e for e in ordered if _ev(e) == "signals_not_converged"]
    self_res_entries = [e for e in ordered if _ev(e) == "pre_execution_self_resolved"]

    out: List[str] = []
    if conv_entries:
        last_conv = conv_entries[-1]
        d = last_conv.details or {}
        conv_data = d.get("convergence") if isinstance(d.get("convergence"), dict) else d
        if not isinstance(conv_data, dict):
            conv_data = {}

        # Merge fallback fields from context if ledger entry pre-dates rich telemetry
        ctx_conv = context.get("convergence_result") if isinstance(context.get("convergence_result"), dict) else {}
        signals = conv_data.get("signals") or ctx_conv.get("signals") or {}
        if isinstance(signals, dict):
            signals = {k: v for k, v in signals.items() if k != "_meta"}
        else:
            signals = {}
        thresholds = conv_data.get("slo_thresholds") or ctx_conv.get("slo_thresholds") or {}
        pod_checks = conv_data.get("pod_checks") or ctx_conv.get("pod_checks") or []
        monitored_svcs = conv_data.get("monitored_services") or ctx_conv.get("monitored_services") or []
        mode = conv_data.get("workload_mode") or ctx_conv.get("workload_mode")
        bake = conv_data.get("bake_time_seconds") if conv_data.get("bake_time_seconds") is not None else ctx_conv.get("bake_time_seconds")
        clean = conv_data.get("clean_samples") if conv_data.get("clean_samples") is not None else ctx_conv.get("clean_samples")
        target = conv_data.get("target_clean_samples") if conv_data.get("target_clean_samples") is not None else ctx_conv.get("target_clean_samples")
        win = conv_data.get("window_seconds") if conv_data.get("window_seconds") is not None else ctx_conv.get("window_seconds")
        poll = conv_data.get("poll_interval_seconds") if conv_data.get("poll_interval_seconds") is not None else ctx_conv.get("poll_interval_seconds")

        out.append(f"- **Convergence Status:** *Confirmed* at `{last_conv.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}`")
        if monitored_svcs:
            svcs_str = ", ".join(f"`{s}`" for s in monitored_svcs)
            mode_str = f" (*{mode}*)" if mode else ""
            out.append(f"- **Monitored Services:** {svcs_str}{mode_str}")
        window_bits: List[str] = []
        if bake is not None:
            window_bits.append(f"bake time `{bake}s`")
        if clean is not None and target is not None:
            window_bits.append(f"`{clean}/{target}` consecutive clean samples")
        if win is not None and poll is not None:
            window_bits.append(f"`{win}s` SLO window (`{poll}s` poll interval)")
        if window_bits:
            out.append(f"- **Verification Window:** {', '.join(window_bits)}")

        if signals or pod_checks:
            out.append("")
            out.extend(_render_signals_and_pods(signals, thresholds, pod_checks))
        elif isinstance(conv_data.get("summary"), str) and conv_data["summary"].strip():
            out.append(f"- **Signals Summary:** {conv_data['summary'].strip()}")
        else:
            out.append("- **Signals Summary:** Convergence passed all SLO checks (per-metric signal values were not recorded in this ledger entry).")

        if not_conv_entries:
            out.append("")
            out.append(f"- **Prior Convergence Attempts:** `{len(not_conv_entries)}` earlier attempt(s) timed out before SLO recovery.")

    elif self_res_entries:
        sr = self_res_entries[-1]
        d = sr.details or {}
        out.append(f"- **Convergence Status:** *Self-Resolved* prior to remediation at `{sr.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}`")
        if d.get("reason"):
            out.append(f"- **Pre-Execution Check:** {d['reason']}")

    else:
        out.append("- **Convergence Status:** *Not Confirmed* — incident did not reach automated SLO convergence.")
        if not_conv_entries:
            last_nc = not_conv_entries[-1]
            d = last_nc.details or {}
            nc_conv = d.get("convergence") if isinstance(d.get("convergence"), dict) else {}
            nc_signals = d.get("signals_at_timeout") or nc_conv.get("signals") or {}
            nc_thresholds = nc_conv.get("slo_thresholds") or {}
            nc_pods = nc_conv.get("pod_checks") or []
            out.append(f"- **Latest Timeout Check:** `{last_nc.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}` ({len(not_conv_entries)} timeout attempt(s))")
            if nc_signals or nc_pods:
                out.append("")
                out.extend(_render_signals_and_pods(nc_signals, nc_thresholds, nc_pods))
        elif isinstance(context.get("convergence_result"), dict) and context["convergence_result"].get("signals"):
            cr = context["convergence_result"]
            out.append("")
            out.extend(_render_signals_and_pods(
                cr.get("signals") or {},
                cr.get("slo_thresholds") or {},
                cr.get("pod_checks") or [],
            ))

    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# Enforcement of deterministic sections onto LLM output
# --------------------------------------------------------------------------- #

def _split_sections(markdown: str) -> List[Tuple[str, str]]:
    """Split into [(heading_line, body)] where first item's heading may be the H1/preamble."""
    parts: List[Tuple[str, str]] = []
    current_head = ""
    buf: List[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            parts.append((current_head, "\n".join(buf)))
            current_head, buf = line, []
        else:
            buf.append(line)
    parts.append((current_head, "\n".join(buf)))
    return parts


def format_root_cause_block(summary_body: str) -> str:
    """Format Root Cause in Summary with separated evidence bullet points."""
    lines = summary_body.splitlines()
    out: List[str] = []
    for line in lines:
        line_s = line.strip()
        if line_s.startswith("- **Root Cause:**") or line_s.startswith("- **Root Cause :**"):
            rc_text = line
            pattern = r"(?:,\s*)?(?:Supporting\s+(?:technical\s+)?evidence\s+includes|evidence\s+includes|evidence:)\s*[:—]?\s*(.*)$"
            m = re.search(pattern, rc_text, flags=re.IGNORECASE)
            if m:
                main_rc = rc_text[:m.start()].strip().rstrip(",;.")
                ev_raw = m.group(1).strip()
                out.append(main_rc)
                out.append("  - *Supporting Evidence:*")
                items = [it.strip().strip(";.") for it in ev_raw.split(";") if it.strip()]
                for item in items:
                    it_fmt = re.sub(r"\b(ready:\s*true|ready:\s*false|restart_count:\s*\d+)\b", r"*\1*", item)
                    it_fmt = re.sub(r"\b(Running|ImagePullBackOff|CrashLoopBackOff)\b", r"*\1*", it_fmt)
                    out.append(f"    - {it_fmt}")
            else:
                out.append(line)
        else:
            out.append(line)
    return "\n".join(out)


def enforce_sections(
    markdown: str,
    *,
    status_badge: str,
    timeline_block: str,
    evolution_block: str,
    actions_block: str,
    recovery_block: Optional[str] = None,
) -> str:
    """Overwrite/insert the deterministic sections so they are always complete and grounded."""
    parts = _split_sections(markdown)
    def idx_of(prefix: str) -> int:
        return next((i for i, (h, _) in enumerate(parts) if h.lower().startswith(f"## {prefix}")), -1)

    # Badge goes directly under the H1 title (preamble body), replacing any earlier badge.
    pre_head, pre_body = parts[0]
    pre_body = re.sub(
        r"^>\s*\*\*.*?(?:STATUS|Incident Status):.*(?:\n>\s*<sub.*?<\/sub>)?\n?",
        "",
        pre_body,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    title_lines = pre_body.strip("\n").splitlines()
    title = title_lines[0] if title_lines else "# Incident Report"
    rest = "\n".join(title_lines[1:]).strip("\n")
    pre_body = f"{title}\n\n{status_badge}" + (f"\n\n{rest}" if rest else "")
    parts[0] = (pre_head, pre_body)

    # Format Root Cause in Summary with separated evidence if present
    si = idx_of("summary")
    if si >= 0:
        s_head, s_body = parts[si]
        parts[si] = (s_head, format_root_cause_block(s_body))

    # Timeline
    ti = idx_of("timeline")
    timeline_body = f"\n{timeline_block}\n"
    if ti >= 0:
        parts[ti] = ("## Timeline", timeline_body)
    else:
        parts.append(("## Timeline", timeline_body))

    # Hypothesis evolution
    hi = idx_of("hypothesis evolution")
    evo_body = f"\n{evolution_block}\n"
    if hi >= 0:
        parts[hi] = ("## Hypothesis Evolution & Operator Steering", evo_body)
    else:
        ai = idx_of("actions taken")
        entry = ("## Hypothesis Evolution & Operator Steering", evo_body)
        parts.insert(ai if ai >= 0 else len(parts), entry)

    # Actions
    ai = idx_of("actions taken")
    actions_body = f"\n{actions_block}\n"
    if ai >= 0:
        parts[ai] = ("## Actions Taken", actions_body)
    else:
        parts.append(("## Actions Taken", actions_body))

    # Recovery Confirmation
    if recovery_block is not None:
        ri = idx_of("recovery confirmation")
        rec_body = f"\n{recovery_block}\n"
        if ri >= 0:
            parts[ri] = ("## Recovery Confirmation", rec_body)
        else:
            rec_i = idx_of("recommendations")
            sc_i = idx_of("scorecard")
            ins_i = rec_i if rec_i >= 0 else (sc_i if sc_i >= 0 else len(parts))
            parts.insert(ins_i, ("## Recovery Confirmation", rec_body))

    out: List[str] = []
    for head, body in parts:
        if head:
            out.append(head)
        out.append(body.rstrip("\n") + "\n" if body.strip() else "")
    return "\n".join(s for s in out if s != "").rstrip() + "\n"
