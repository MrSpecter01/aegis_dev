"""LangGraph Scribe Reasoning Graph.

Orchestrates the 4-phase reporting lifecycle:
1. load_ledger: Fetch and structure append-only incident ledger from Redis.
2. generate_report: Synthesize incident report via Scribe LLM.
3. validate_report: Deterministic evaluation and anti-hallucination check with reflection loop.
4. deliver_report: Persist to disk, send Telegram document, and callback API state machine.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import StateGraph, END
import redis

from services.api_server.incident_store import get_incident
from services.phase3_worker.delivery import deliver_report
from services.phase3_worker.report_builder import (
    build_actions_section,
    build_hypothesis_evolution,
    build_recovery_section,
    build_status_badge,
    build_timeline_narrative,
    build_timeline_table,
    collect_actions,
    enforce_sections,
)
from services.phase3_worker.validator import validate_incident_report
from shared.harness.llm import build_llm
from shared.ledger.writer import get_ledger_entries
from shared.models.ledger import IncidentReport, LedgerEntry
from shared.prompts.loader import get_prompts_dir

logger = logging.getLogger("phase3_worker.graph")


class ScribeGraphState(TypedDict, total=False):
    """LangGraph state representation for the Phase 3 Scribe Agent."""
    incident_id: str
    severity: str
    affected_service: Optional[str]
    affected_namespace: Optional[str]
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    ledger_entries: List[LedgerEntry]
    context: Dict[str, Any]
    topology_snapshot: Optional[Dict[str, Any]]
    detected_gaps: List[str]
    report_markdown: Optional[str]
    report_dict: Optional[Dict[str, Any]]
    incident_report: Optional[IncidentReport]
    is_valid: bool
    feedback: Optional[str]
    retry_count: int
    delivery_result: Optional[Dict[str, Any]]
    error: Optional[str]


def load_scribe_prompts(prompts_dir: Optional[Path] = None) -> Tuple[str, str]:
    """Load system prompt and report template from seed/prompts."""
    base_dir = get_prompts_dir(prompts_dir)
    sys_path = base_dir / "system_scribe_agent.md"
    tmpl_path = base_dir / "incident_report_template.md"

    system_prompt = (
        sys_path.read_text(encoding="utf-8")
        if sys_path.is_file()
        else "You are the Aegis Scribe Agent. Generate an accurate post-incident report from provided ledger entries."
    )
    template = (
        tmpl_path.read_text(encoding="utf-8")
        if tmpl_path.is_file()
        else "# Incident Report — {incident_id}\n\n## Summary\n- **Root Cause:** {root_cause_hypothesis}"
    )
    return system_prompt, template


def format_timeline_table(entries: List[LedgerEntry]) -> Tuple[str, List[str]]:
    """Format ledger entries into a humanised Markdown table and detect unrecorded gaps > 300s."""
    return build_timeline_table(entries)


def create_scribe_graph(
    llm: Optional[Any] = None,
    redis_client: Optional[redis.Redis] = None,
    delivery_fn: Optional[Callable[..., Any]] = None,
    prompts_dir: Optional[Path] = None,
    checkpointer: Optional[Any] = None,
):
    """Construct and compile the LangGraph Phase 3 Scribe workflow."""

    async def load_ledger_node(state: ScribeGraphState) -> ScribeGraphState:
        """Fetch and deserialize append-only ledger entries and incident context."""
        incident_id = state.get("incident_id", "unknown")
        entries = list(state.get("ledger_entries", []))

        # Hydrate from Redis if not pre-populated
        if not entries and redis_client is not None:
            entries = get_ledger_entries(redis_client, incident_id)

        # Hydrate incident metadata if available
        severity = state.get("severity")
        affected_service = state.get("affected_service")
        affected_namespace = state.get("affected_namespace")
        context = dict(state.get("context", {}))

        if redis_client is not None:
            inc_meta = get_incident(redis_client, incident_id) or {}
            if not severity:
                severity = inc_meta.get("severity", "P2")
            if not affected_service:
                affected_service = inc_meta.get("affected_service") or inc_meta.get("service", "unknown-service")
            if not affected_namespace:
                affected_namespace = inc_meta.get("affected_namespace") or inc_meta.get("namespace", "default")

            raw_ctx = redis_client.get(f"incident:{incident_id}:context")
            if raw_ctx:
                try:
                    if isinstance(raw_ctx, bytes):
                        raw_ctx = raw_ctx.decode("utf-8")
                    context.update(json.loads(raw_ctx))
                except Exception as e:
                    logger.warning("Could not parse incident context for %s: %s", incident_id, e)

        # Format timeline and detect gaps
        timeline_table, gaps = format_timeline_table(entries)

        # Calculate time boundaries
        sorted_entries = sorted(entries, key=lambda e: e.timestamp) if entries else []
        start_time = sorted_entries[0].timestamp if sorted_entries else datetime.now(timezone.utc)
        end_time = sorted_entries[-1].timestamp if sorted_entries else datetime.now(timezone.utc)

        return {
            **state,
            "incident_id": incident_id,
            "severity": severity or "P2",
            "affected_service": affected_service or "unknown-service",
            "affected_namespace": affected_namespace or "default",
            "ledger_entries": entries,
            "context": context,
            "detected_gaps": gaps,
            "start_time": start_time,
            "end_time": end_time,
        }

    async def generate_report_node(state: ScribeGraphState) -> ScribeGraphState:
        """Generate incident report via Scribe LLM adhering to template & instructions."""
        incident_id = state.get("incident_id", "unknown")
        severity = state.get("severity", "P2")
        service = state.get("affected_service", "unknown-service")
        namespace = state.get("affected_namespace", "default")
        start_time = state.get("start_time", datetime.now(timezone.utc))
        end_time = state.get("end_time", datetime.now(timezone.utc))
        duration_sec = max(0.0, (end_time - start_time).total_seconds())

        entries = state.get("ledger_entries", [])
        timeline_table, _ = format_timeline_table(entries)
        context = state.get("context", {})

        # Load prompts
        sys_prompt, template = load_scribe_prompts(prompts_dir)

        # Extract findings & action evidence
        evidence_summary_lines = []
        if context.get("primary_hypothesis"):
            evidence_summary_lines.append(f"- **Primary Hypothesis:** {context.get('primary_hypothesis')}")

        all_ev = context.get("all_evidence") or context.get("evidence") or []
        if not all_ev and isinstance(context.get("merged_findings"), dict):
            all_ev = context.get("merged_findings", {}).get("all_evidence", []) or context.get("merged_findings", {}).get("ranked_evidence", [])

        for ev in all_ev:
            if isinstance(ev, dict):
                src = ev.get("source_tool") or ev.get("signal_type", "signal")
                query = ev.get("query_or_cmd") or ev.get("description", "")
                raw = str(ev.get("raw_output") or ev.get("peak_value", ""))[:140]
                evidence_summary_lines.append(f"- `{src}`: {query} → {raw}")
            elif isinstance(ev, str):
                evidence_summary_lines.append(f"- {ev[:140]}")

        ranked_findings = context.get("ranked_findings") or []
        if isinstance(ranked_findings, list) and ranked_findings:
            for idx, f in enumerate(ranked_findings, start=1):
                if isinstance(f, dict):
                    role = f.get("agent_role", "unknown")
                    hyp = f.get("hypothesis", "") or "no tools called, 0 evidence"
                    conf = f.get("confidence", 0)
                    ev_cnt = len(f.get("evidence") or [])
                    status_note = "no tools called, 0 evidence" if ev_cnt == 0 and ("no tools called" in hyp.lower() or "[ungrounded" in hyp.lower()) else f"{ev_cnt} evidence items"
                    evidence_summary_lines.append(f"- Rank {idx} `{role}` [{status_note}] (confidence: {conf}): {hyp}")
        elif isinstance(context.get("findings_by_agent"), dict):
            for role, f in context.get("findings_by_agent", {}).items():
                if isinstance(f, dict):
                    hyp = f.get("hypothesis", "") or "no tools called, 0 evidence"
                    conf = f.get("confidence", 0)
                    ev_cnt = len(f.get("evidence") or [])
                    status_note = "no tools called, 0 evidence" if ev_cnt == 0 and ("no tools called" in hyp.lower() or "[ungrounded" in hyp.lower()) else f"{ev_cnt} evidence items"
                    evidence_summary_lines.append(f"- `{role}` [{status_note}] (confidence: {conf}): {hyp}")
                    if not all_ev:
                        for sub_ev in f.get("evidence", []):
                            if isinstance(sub_ev, dict):
                                evidence_summary_lines.append(f"  * `{sub_ev.get('source_tool')}`: {sub_ev.get('query_or_cmd')} → {str(sub_ev.get('raw_output'))[:120]}")

        evidence_summary = "\n".join(evidence_summary_lines) if evidence_summary_lines else "- [no record]"

        # Format actions taken
        actions = context.get("action_proposal", {}).get("actions", []) or context.get("actions", [])
        if not actions and redis_client is not None:
            try:
                raw_prop = redis_client.get(f"incident:{incident_id}:proposal") or redis_client.get(f"incident:{incident_id}:action")
                if raw_prop:
                    p_data = json.loads(raw_prop) if isinstance(raw_prop, str) else json.loads(raw_prop.decode("utf-8"))
                    if isinstance(p_data, dict):
                        inner_actions = p_data.get("actions") or p_data.get("proposal", {}).get("actions", [])
                        if inner_actions:
                            actions = inner_actions
            except Exception:
                pass

        actions_lines = []
        prop_cits = context.get("action_proposal", {}).get("runbook_citations") or []
        for act in actions:
            if isinstance(act, dict):
                tool = act.get("tool_name", "action")
                target = act.get("target_resource", "")
                params = json.dumps(act.get("parameters", {}))
                act_cits = act.get("runbook_citations") or prop_cits
                cit_suffix = ""
                if isinstance(act_cits, list) and act_cits:
                    cit_strs = []
                    for c in act_cits:
                        if isinstance(c, dict):
                            b = c.get("book_name") or c.get("runbook_id") or "Runbook"
                            s = c.get("section") or "General"
                            loc = ", ".join(p for p in (c.get("line_range", ""), c.get("chunk_id", "")) if p)
                            cit_strs.append(f"{b} §{s}" + (f" ({loc})" if loc else ""))
                    if cit_strs:
                        cit_suffix = f" [Citations: {'; '.join(cit_strs)}]"
                actions_lines.append(f"- `{tool}` on `{target}` (params: {params}) — outcome: completed{cit_suffix}")
            elif isinstance(act, str):
                actions_lines.append(f"- {act}")

        # Also inspect ledger entries for executed actions if actions list was empty
        if not actions_lines:
            for entry in entries:
                if getattr(entry, "event", "") in ("action_executed", "action_proposed", "approved"):
                    payload = getattr(entry, "payload", {})
                    if isinstance(payload, dict):
                        p_act = payload.get("action") or payload.get("action_id") or payload.get("tool_name")
                        if p_act:
                            actions_lines.append(f"- Action event `{entry.event}`: {p_act}")

        actions_summary = "\n".join(actions_lines) if actions_lines else "- [no record]"

        # Deterministic, ledger-grounded sections (every action, re-plans, steering, status, recovery)
        if collect_actions(entries):
            actions_summary = build_actions_section(entries)
        status_badge = build_status_badge(entries)
        timeline_narrative = build_timeline_narrative(entries)
        timeline_block = f"{timeline_narrative}\n\n{timeline_table}"
        evolution_block = build_hypothesis_evolution(entries, context)
        recovery_block = build_recovery_section(entries, context)

        # Format convergence
        conv_res = context.get("convergence_result", {})
        conv_time = conv_res.get("converged_at", end_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
        signals_summary = recovery_block

        # Resolve LLM client
        active_llm = llm or build_llm("scribe")

        # Build prompt
        reflection_feedback = state.get("feedback")
        feedback_block = ""
        if reflection_feedback:
            feedback_block = (
                f"\n\n[PREVIOUS ATTEMPT VALIDATION FAILURES — MUST FIX]\n"
                f"{reflection_feedback}\n"
                f"Ensure ALL failures above are resolved in this generation."
            )

        # Compute grounded metrics from ledger entries
        sorted_entries = sorted(entries, key=lambda e: e.timestamp) if entries else []
        triage_sec = 45.0
        operator_wait_sec = 0.0

        if sorted_entries:
            e_start = sorted_entries[0].timestamp
            for e in sorted_entries:
                ev_name = getattr(e, "event_type", "") or getattr(e, "event", "")
                if ev_name in ("triage_complete", "merge_complete"):
                    triage_sec = max(1.0, (e.timestamp - e_start).total_seconds())
                    break

            last_proposed_ts = None
            for e in sorted_entries:
                ev_name = getattr(e, "event_type", "") or getattr(e, "event", "")
                if ev_name == "action_proposed":
                    last_proposed_ts = e.timestamp
                elif ev_name in ("approved", "denied", "steer") and last_proposed_ts:
                    diff = (e.timestamp - last_proposed_ts).total_seconds()
                    if diff > 0:
                        operator_wait_sec += diff
                    last_proposed_ts = None

        autonomous_mttr_sec = max(0.0, duration_sec - operator_wait_sec)

        user_prompt = (
            f"Generate the full post-incident report for incident '{incident_id}' by filling the template below.\n\n"
            f"<incident_data>\n"
            f"Incident ID: {incident_id}\n"
            f"Severity: {severity}\n"
            f"Service: {service} ({namespace})\n"
            f"Duration: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')} ({duration_sec:.1f}s)\n"
            f"Triage Duration: {triage_sec:.1f}s\n"
            f"Recovery Duration: {duration_sec:.1f}s\n"
            f"Autonomous MTTR: {autonomous_mttr_sec:.1f}s (Operator Hold: {operator_wait_sec:.1f}s)\n"
            f"Evidence: {evidence_summary}\n"
            f"Actions: {actions_summary}\n"
            f"Convergence: {conv_time} — {signals_summary}\n"
            f"Status Badge:\n{status_badge}\n"
            f"Timeline (narrative + table):\n{timeline_block}\n"
            f"Hypothesis Evolution:\n{evolution_block}\n"
            f"</incident_data>\n\n"
            f"<template>\n{template}\n</template>"
            f"{feedback_block}"
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            if hasattr(active_llm, "ainvoke"):
                resp = await active_llm.ainvoke(messages)
            else:
                resp = active_llm.invoke(messages)
            raw_content = resp.content if hasattr(resp, "content") else str(resp)
            if isinstance(raw_content, list):
                text_parts = []
                for part in raw_content:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict):
                        # Filter out thinking/reasoning blocks
                        if part.get("type") == "thinking" or "thinking" in part:
                            continue
                        if "text" in part and part["text"]:
                            text_parts.append(str(part["text"]))
                raw_content = "".join(text_parts)
            elif not isinstance(raw_content, str):
                raw_content = str(raw_content)

            # Strip any raw thinking blocks or <think> tags if present in string
            if isinstance(raw_content, str):
                import re
                raw_content = re.sub(r"^\{['\"]type['\"]:\s*['\"]thinking['\"].*?\}\s*", "", raw_content, flags=re.DOTALL)
                raw_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
        except Exception as exc:
            logger.error("LLM generation error: %s", exc)
            raw_content = ""

        # Fallback / sanitize if output is empty
        if not raw_content or len(raw_content.strip()) < 50:
            # Deterministic fallback template filling
            raw_content = template.format(
                incident_id=incident_id,
                severity=severity,
                start_time=start_time.strftime("%H:%M:%S"),
                resolved_time=end_time.strftime("%H:%M:%S"),
                duration=f"{duration_sec:.1f}s",
                affected_services=f"{service}.{namespace}",
                root_cause_hypothesis=f"Suspected resource exhaustion in {service} supported by telemetry",
                status_badge=status_badge,
                timeline_table=timeline_block,
                hypothesis_evolution=evolution_block,
                ranked_evidence_summary=evidence_summary,
                action_plan_with_outcomes=actions_summary,
                convergence_timestamp=conv_time,
                signals_summary=signals_summary,
                recommendations=f"- Review resource limits for {service}\n- Add alerting thresholds",
                triage_duration=f"{triage_sec:.1f}",
                recovery_duration=f"{duration_sec:.1f}",
            )

        # Enforce deterministic sections regardless of LLM output (complete + grounded)
        raw_content = enforce_sections(
            raw_content,
            status_badge=status_badge,
            timeline_block=timeline_block,
            evolution_block=evolution_block,
            actions_block=actions_summary,
            recovery_block=recovery_block,
        )

        # Build structured dictionary for Pydantic model
        report_dict = {
            "incident_id": incident_id,
            "title": f"Incident {incident_id} — {service}",
            "severity": severity,
            "affected_service": service,
            "affected_namespace": namespace,
            "start_time": start_time,
            "end_time": end_time,
            "duration_seconds": duration_sec,
            "root_cause_summary": f"Suspected issue in {service}",
            "remediation_summary": actions_summary,
            "timeline": entries,
            "action_log": actions,
            "preventative_recommendations": [f"Tune resource limits for {service}"],
        }

        return {
            **state,
            "report_markdown": raw_content,
            "report_dict": report_dict,
        }

    async def validate_report_node(state: ScribeGraphState) -> ScribeGraphState:
        """Deterministic validation of generated report across 6 checks."""
        markdown_text = state.get("report_markdown", "")
        ledger_entries = state.get("ledger_entries", [])
        report_dict = state.get("report_dict", {})

        result = validate_incident_report(
            markdown_text=markdown_text,
            ledger_entries=ledger_entries,
            report_data=report_dict,
        )

        retry_count = state.get("retry_count", 0)

        if result.is_valid:
            logger.info("Report validation passed for incident %s", state.get("incident_id"))
            return {
                **state,
                "is_valid": True,
                "feedback": None,
                "incident_report": result.validated_report,
            }
        else:
            logger.warning(
                "Report validation failed for %s (attempt %d/2): %s",
                state.get("incident_id"),
                retry_count + 1,
                result.errors,
            )
            return {
                **state,
                "is_valid": False,
                "feedback": result.feedback_prompt,
                "retry_count": retry_count + 1,
                "incident_report": result.validated_report,
            }

    def should_reflect(state: ScribeGraphState) -> str:
        """Routing conditional edge: deliver if valid, retry if invalid and budget remaining."""
        if state.get("is_valid"):
            return "deliver_report"
        if state.get("retry_count", 0) <= 2:
            return "generate_report"
        logger.error("Max retries reached in validation; proceeding to deliver with safe fallback.")
        return "deliver_report"

    async def deliver_report_node(state: ScribeGraphState) -> ScribeGraphState:
        """Deliver report artifacts to disk, Telegram document, and API callback."""
        incident_id = state.get("incident_id", "unknown")
        report = state.get("incident_report")

        # Fallback if validation did not produce an IncidentReport instance
        if not report:
            markdown_content = state.get("report_markdown", "")
            report = IncidentReport(
                incident_id=incident_id,
                title=f"Incident {incident_id}",
                severity=state.get("severity", "P2"),
                affected_service=state.get("affected_service", "unknown"),
                affected_namespace=state.get("affected_namespace", "default"),
                start_time=state.get("start_time", datetime.now(timezone.utc)),
                end_time=state.get("end_time", datetime.now(timezone.utc)),
                duration_seconds=max(0.0, (state.get("end_time", datetime.now(timezone.utc)) - state.get("start_time", datetime.now(timezone.utc))).total_seconds()),
                root_cause_summary="Hypothesized root cause per findings",
                remediation_summary="Actions executed",
                timeline=state.get("ledger_entries", []),
                action_log=[],
                preventative_recommendations=[],
                markdown_content=markdown_content,
            )

        delivery_result = {}
        if delivery_fn is not None:
            delivery_result = await delivery_fn(incident_id, report, state.get("ledger_entries", []))
        else:
            delivery_result = await deliver_report(
                incident_id=incident_id,
                report=report,
                ledger_entries=state.get("ledger_entries", []),
                topology_snapshot=state.get("topology_snapshot"),
            )

        return {
            **state,
            "incident_report": report,
            "delivery_result": delivery_result,
        }

    # Build LangGraph StateGraph
    builder = StateGraph(ScribeGraphState)
    builder.add_node("load_ledger", load_ledger_node)
    builder.add_node("generate_report", generate_report_node)
    builder.add_node("validate_report", validate_report_node)
    builder.add_node("deliver_report", deliver_report_node)

    builder.set_entry_point("load_ledger")
    builder.add_edge("load_ledger", "generate_report")
    builder.add_edge("generate_report", "validate_report")
    builder.add_conditional_edges(
        "validate_report",
        should_reflect,
        {
            "generate_report": "generate_report",
            "deliver_report": "deliver_report",
        },
    )
    builder.add_edge("deliver_report", END)

    return builder.compile(checkpointer=checkpointer)
