from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.types import interrupt
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from datetime import datetime, timezone

from shared.config.loader import ConfigManager
from shared.models.enums import IncidentState
from shared.models.ledger import LedgerEntry
from shared.ledger.writer import append_ledger_entry
from shared.models.actions import ActionProposal, ActionPlanItem, RunbookCitation
from shared.harness.llm import build_llm
from shared.harness.mcp import resolve_mcp_client_for_tool
from services.mcp.tools.qdrant import qdrant_search
from services.phase2_worker.convergence import run_convergence_monitor
from services.phase2_worker.runbook_index import (
    load_runbooks_index,
    extract_operational_rules,
    format_catalog_prompt,
    resolve_runbook_section_metadata,
    should_use_two_step_catalog,
    search_runbook_catalog,
)

logger = logging.getLogger(__name__)


class Phase2GraphState(TypedDict, total=False):
    incident_id: str
    severity: str
    affected_service: str
    affected_namespace: Optional[str]
    merged_finding: Dict[str, Any]
    runbook_index: Optional[List[Dict[str, Any]]]
    retrieved_runbooks: List[Dict[str, Any]]
    action_proposal: Optional[Dict[str, Any]]
    prior_attempts: List[Dict[str, Any]]
    steering_instructions: Optional[str]
    pre_approved: bool
    action_id: Optional[str]
    approval_status: Optional[str]  # "approved", "denied", "steer"
    execution_results: List[Dict[str, Any]]
    convergence_result: Optional[Dict[str, Any]]
    outcome: Optional[str]  # "action_proposed", "low_confidence", "signals_converged", "signals_not_converged", "escalated", "denied", "execution_failed", "pre_exec_replan"
    signals_at_timeout: Optional[Dict[str, Any]]
    retry_count: int
    error: Optional[str]
    mutation_delta: Optional[str]
    pre_exec_replan_count: int
    gate_verdict: Optional[str]
    react_turns_used: int


def load_system_prompt() -> str:
    """Load remediation agent system prompt from seed/prompts."""
    prompt_path = Path("seed/prompts/system_remediation_agent.md")
    if prompt_path.is_file():
        return prompt_path.read_text(encoding="utf-8")
    return "You are the Aegis Remediation Agent. Formulate a targeted ActionProposal based on evidence and runbooks."


class FaultClass(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED_LOAD = "DEGRADED_LOAD"
    IMAGE_PULL_FAILURE = "IMAGE_PULL_FAILURE"
    CONFIG_ERROR = "CONFIG_ERROR"
    OOM_KILLED = "OOM_KILLED"
    APP_CRASH_LOOP = "APP_CRASH_LOOP"
    UNKNOWN = "UNKNOWN"


class PreExecVerdict(StrEnum):
    RESOLVED_SKIP = "RESOLVED_SKIP"
    MUTATION_DETECTED_REPLAN = "MUTATION_DETECTED_REPLAN"
    FAULT_CONFIRMED_PROCEED = "FAULT_CONFIRMED_PROCEED"


# Static tool contraindications: tools that are futile/incompatible for specific root faults
STATIC_CONTRAINDICATIONS: Dict[str, Set[FaultClass]] = {
    "scale_deployment": {FaultClass.IMAGE_PULL_FAILURE, FaultClass.CONFIG_ERROR},
    "update_resource_limits": {FaultClass.IMAGE_PULL_FAILURE, FaultClass.CONFIG_ERROR},
    "restart_pod": {FaultClass.IMAGE_PULL_FAILURE, FaultClass.CONFIG_ERROR},
}

# Disruptive recovery tools that should be withheld when the service is already healthy
DISRUPTIVE_RECOVERY_TOOLS: Set[str] = {"restart_pod", "rollback_release"}

# Keywords indicating hung/deadlocked processes where container reports Running but is frozen
HUNG_PROCESS_KEYWORDS: Set[str] = {
    "hang", "hung", "deadlock", "unresponsive", "timeout", "rpc", "connection refused", "freeze", "stuck"
}


def classify_live_fault_signature(diag: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract counter-invariant qualitative fault signature from k8s_get_pod_diagnostics.
    Strips monotonic counters (restart_count, event.count, timestamps) and maps
    to a canonical FaultClass:
    - IMAGE_PULL_FAILURE: ImagePullBackOff, ErrImagePull, InvalidImageName
    - CONFIG_ERROR: CreateContainerConfigError, CreateContainerError
    - OOM_KILLED: exit_code 137 or OOMKilled
    - APP_CRASH_LOOP: CrashLoopBackOff or non-zero exit code (not OOM)
    - HEALTHY: all pods Running and Ready (0 unready, 0 waiting errors)
    - DEGRADED_LOAD: Running but elevated load / events, no fatal waiting/terminated state
    """
    if not isinstance(diag, dict) or not diag.get("found") or diag.get("pod_count", 0) == 0:
        return {
            "failure_class": FaultClass.UNKNOWN,
            "detail": "Pod not found or diagnostic unavailable",
            "ready_pods": 0,
            "total_pods": 0,
        }

    pods = diag.get("pods", [])
    total_pods = len(pods)
    ready_containers = 0
    total_containers = 0

    waiting_reasons = set()
    term_reasons = set()
    exit_codes = set()
    critical_issues = list(diag.get("critical_issues", []))

    for p in pods:
        for c in p.get("containers", []):
            total_containers += 1
            if c.get("ready"):
                ready_containers += 1
            w_r = c.get("waiting_reason")
            if w_r:
                waiting_reasons.add(str(w_r))
            t_r = c.get("terminated_reason")
            if t_r:
                term_reasons.add(str(t_r))
            e_c = c.get("exit_code")
            if e_c is not None:
                try:
                    exit_codes.add(int(e_c))
                except (ValueError, TypeError):
                    pass

    # Check for image pull errors
    image_errs = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ImageInspectError"}
    if waiting_reasons.intersection(image_errs) or any("image" in iss.lower() for iss in critical_issues):
        return {
            "failure_class": FaultClass.IMAGE_PULL_FAILURE,
            "detail": f"Container waiting on image pull ({', '.join(waiting_reasons.intersection(image_errs)) or 'ImagePullBackOff'})",
            "ready_pods": ready_containers,
            "total_pods": total_pods,
        }

    # Check for container config errors
    config_errs = {"CreateContainerConfigError", "CreateContainerError"}
    if waiting_reasons.intersection(config_errs):
        return {
            "failure_class": FaultClass.CONFIG_ERROR,
            "detail": f"Container config error ({', '.join(waiting_reasons.intersection(config_errs))})",
            "ready_pods": ready_containers,
            "total_pods": total_pods,
        }

    # Check for OOMKilled (must be explicit OOMKilled reason or event, not generic exit 137 from SIGKILL/liveness)
    if "OOMKilled" in term_reasons or any("oomkilled" in iss.lower() for iss in critical_issues):
        return {
            "failure_class": FaultClass.OOM_KILLED,
            "detail": "Container terminated with OOMKilled",
            "ready_pods": ready_containers,
            "total_pods": total_pods,
        }

    # Check for CrashLoopBackOff or non-zero exit codes
    if "CrashLoopBackOff" in waiting_reasons or any("crashloop" in iss.lower() for iss in critical_issues) or any(ec != 0 for ec in exit_codes):
        return {
            "failure_class": FaultClass.APP_CRASH_LOOP,
            "detail": "Container in CrashLoopBackOff or abnormal termination",
            "ready_pods": ready_containers,
            "total_pods": total_pods,
        }

    # Check for healthy state
    if ready_containers == total_containers and total_containers > 0 and not waiting_reasons and not term_reasons and not critical_issues:
        return {
            "failure_class": FaultClass.HEALTHY,
            "detail": f"All containers Running and Ready ({ready_containers}/{total_containers})",
            "ready_pods": ready_containers,
            "total_pods": total_pods,
        }

    return {
        "failure_class": FaultClass.DEGRADED_LOAD,
        "detail": f"Container running with degraded state ({ready_containers}/{total_containers} ready)",
        "ready_pods": ready_containers,
        "total_pods": total_pods,
    }


def evaluate_pre_execution_gate(
    tool_name: str,
    tool_params: Dict[str, Any],
    diag: Dict[str, Any],
    replan_count: int = 0,
    max_replans: int = 1,
    hypothesis: str = "",
) -> Dict[str, Any]:
    """
    Evaluate Pre-Execution Gate:
    1. RESOLVED_SKIP: Transient condition subsided; pods 1/1 Ready and healthy. Skip disruptive tool to avoid churn.
    2. FAULT_CONFIRMED_PROCEED: Active failure class verified and matches approved tool; proceed with execution.
    3. MUTATION_DETECTED_REPLAN: Condition evolved into an incompatible failure class (e.g. Traffic -> ImagePullBackOff).
       Abort tool and trigger re-plan.
    """
    sig = classify_live_fault_signature(diag)
    raw_class = sig.get("failure_class", FaultClass.UNKNOWN)
    try:
        f_class = FaultClass(raw_class)
    except ValueError:
        f_class = FaultClass.UNKNOWN

    f_detail = sig.get("detail", "")
    ready_pods = sig.get("ready_pods", 0)

    # 1. Check for RESOLVED_SKIP:
    if f_class == FaultClass.HEALTHY:
        # If action is restart_pod, but hypothesis indicates an unresponsive, hung, or deadlocked process,
        # the container status 'Running' is expected (process alive but not serving). Must proceed to bounce!
        is_hung_condition = any(w in hypothesis.lower() for w in HUNG_PROCESS_KEYWORDS)
        if tool_name == "restart_pod" and is_hung_condition:
            return {
                "verdict": PreExecVerdict.FAULT_CONFIRMED_PROCEED,
                "reason": f"Service runtime is unresponsive/deadlocked per hypothesis despite running status ({f_detail}). Action 'restart_pod' confirmed to clear stuck process.",
                "signature": sig,
            }

        if tool_name in DISRUPTIVE_RECOVERY_TOOLS:
            return {
                "verdict": PreExecVerdict.RESOLVED_SKIP,
                "reason": f"Live pre-execution diagnostics confirm service is healthy ({f_detail}). Action '{tool_name}' skipped to prevent disruption.",
                "signature": sig,
            }

    # 2. Check for MUTATION_DETECTED_REPLAN (Action-to-Fault Incompatibility):
    if replan_count < max_replans:
        # Check declarative static contraindications
        if f_class in STATIC_CONTRAINDICATIONS.get(tool_name, set()):
            contraindication_reasons = {
                "scale_deployment": f"Approved action '{tool_name}' cannot resolve {f_class} ({f_detail}). Scaling a broken deployment only multiplies failed pods.",
                "update_resource_limits": f"Approved action '{tool_name}' cannot resolve {f_class} ({f_detail}). Resource adjustments cannot fix an invalid container image or missing config.",
                "restart_pod": f"Approved action '{tool_name}' cannot resolve {f_class} ({f_detail}). Restarting a pod stuck in {f_class} will immediately re-enter error state.",
            }
            reason = contraindication_reasons.get(
                tool_name,
                f"Approved action '{tool_name}' is contraindicated for failure class {f_class} ({f_detail}).",
            )
            return {
                "verdict": PreExecVerdict.MUTATION_DETECTED_REPLAN,
                "reason": reason,
                "signature": sig,
            }

        # Check contextual rule: scaling when all pods are dead in crashloop/OOM
        if tool_name == "scale_deployment" and ready_pods == 0 and f_class in (FaultClass.OOM_KILLED, FaultClass.APP_CRASH_LOOP):
            return {
                "verdict": PreExecVerdict.MUTATION_DETECTED_REPLAN,
                "reason": f"Approved action '{tool_name}' cannot resolve {f_class} when 0 pods are ready ({f_detail}). Root cause requires memory limit or image rollback.",
                "signature": sig,
            }

    # 3. Default: FAULT_CONFIRMED_PROCEED
    return {
        "verdict": PreExecVerdict.FAULT_CONFIRMED_PROCEED,
        "reason": f"Pre-execution diagnostics verified active condition {f_class} ({f_detail}). Approved action '{tool_name}' is valid for this fault condition.",
        "signature": sig,
    }


# Backward compatibility alias
evaluate_pre_exec_gate = evaluate_pre_execution_gate


def _format_citation_inline_ref(cit: Dict[str, Any]) -> str:
    """Format a structured multi-line runbook reference block for action/proposal rationales."""
    b_name = str(cit.get("book_name") or cit.get("runbook_id") or "Runbook").strip()
    r_id = str(cit.get("runbook_id") or "").strip()
    sec = str(cit.get("section") or "General").strip()
    l_range = str(cit.get("line_range") or "").strip()
    c_id = str(cit.get("chunk_id") or "").strip()
    excerpt = " ".join(str(cit.get("referenced_text") or "").split())

    id_title = (
        f"{r_id}/{b_name}"
        if (r_id and b_name and r_id.lower() != b_name.lower())
        else (r_id or b_name)
    )
    sec_loc = f"{sec}/{l_range}" if l_range else sec
    if c_id:
        sec_loc += f" ({c_id})"

    lines = [
        "[Runbook Reference]",
        f"id/title: '{id_title}'",
        f"section/line_range: '{sec_loc}'",
    ]
    if excerpt:
        short_ex = excerpt if len(excerpt) <= 180 else excerpt[:177].rstrip() + "..."
        lines.append(f'reference: "{short_ex}"')
    return "\n".join(lines)


def _enrich_proposal_citations(
    proposal_dict: Dict[str, Any],
    retrieved_runbooks: Optional[List[Dict[str, Any]]] = None,
    index: Optional[List[Dict[str, Any]]] = None,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure ActionProposal and its actions carry validated RunbookCitation metadata and inline text references."""
    if not isinstance(proposal_dict, dict):
        return proposal_dict

    out = dict(proposal_dict)
    retrieved = [r for r in (retrieved_runbooks or []) if isinstance(r, dict)]
    actions = [dict(a) if isinstance(a, dict) else a for a in (out.get("actions") or [])]
    act_tools = [a.get("tool_name", "") for a in actions if isinstance(a, dict) and a.get("tool_name")]

    def _fill_citation(raw_c: Any, tool_hint: str = "") -> Optional[Dict[str, Any]]:
        try:
            c = RunbookCitation.model_validate(raw_c).model_dump(mode="json")
        except Exception:
            return None

        c_id_low = c["runbook_id"].lower()
        c_book_low = c["book_name"].lower()
        c_sec_low = c["section"].lower()

        # 1. Enrich from dynamically retrieved Qdrant chunks
        best_chunk: Optional[Dict[str, Any]] = None
        for ch in retrieved:
            ch_id = str(ch.get("runbook_id") or "").strip().lower()
            ch_title = str(ch.get("title") or "").strip().lower()
            ch_sec = str(ch.get("section") or "").strip().lower()
            id_match = (c_id_low and ch_id == c_id_low) or (c_book_low and ch_title == c_book_low)
            if not id_match:
                continue
            if c_sec_low and c_sec_low != "general" and ch_sec == c_sec_low:
                best_chunk = ch
                break
            if best_chunk is None:
                best_chunk = ch

        if best_chunk is not None:
            if not c["runbook_id"]:
                c["runbook_id"] = str(best_chunk.get("runbook_id") or "")
            if not c["book_name"]:
                c["book_name"] = str(best_chunk.get("title") or c["runbook_id"])
            if not c["section"] or c["section"] == "General":
                c["section"] = str(best_chunk.get("section") or "General")
            if not c["line_range"]:
                l_s = best_chunk.get("line_start")
                l_e = best_chunk.get("line_end")
                c["line_range"] = str(
                    best_chunk.get("line_range") or (f"L{l_s}-L{l_e}" if l_s and l_e else "")
                )
            if not c["chunk_id"]:
                c_idx = best_chunk.get("chunk_index")
                t_c = best_chunk.get("total_chunks")
                c["chunk_id"] = str(
                    best_chunk.get("chunk_id")
                    or best_chunk.get("chunk_label")
                    or (f"chunk {c_idx}/{t_c}" if c_idx and t_c else "")
                )
            if not c["referenced_text"]:
                c["referenced_text"] = " ".join(str(best_chunk.get("content") or "").split())[:240]

        # 2. Enrich missing metadata from disk / runbooks_index
        if not c["book_name"] or not c["line_range"] or not c["chunk_id"] or not c["referenced_text"]:
            resolved = resolve_runbook_section_metadata(
                runbook_id=c["runbook_id"] or c["book_name"],
                section="" if c["section"] == "General" else c["section"],
                namespace=namespace,
                index=index,
                tool_name=tool_hint,
            )
            if resolved:
                for fld in ("runbook_id", "book_name", "section", "line_range", "chunk_id", "referenced_text"):
                    if (not c.get(fld) or (fld == "section" and c[fld] == "General")) and resolved.get(fld):
                        c[fld] = str(resolved[fld])

        if not (c["runbook_id"] or c["book_name"] or c["referenced_text"]):
            return None
        return c

    # Gather & normalize any existing citations emitted by LLM
    normalized_top: List[Dict[str, Any]] = []
    for raw_c in (out.get("runbook_citations") or []):
        filled = _fill_citation(raw_c, tool_hint=act_tools[0] if act_tools else "")
        if filled:
            normalized_top.append(filled)

    for act in actions:
        if not isinstance(act, dict):
            continue
        act_tool = str(act.get("tool_name") or "")
        act_cits: List[Dict[str, Any]] = []
        for raw_c in (act.get("runbook_citations") or []):
            filled = _fill_citation(raw_c, tool_hint=act_tool)
            if filled:
                act_cits.append(filled)
                normalized_top.append(filled)
        act["runbook_citations"] = act_cits

    # Fallback derivation if LLM omitted structured runbook_citations
    if not normalized_top:
        seen_chunk_keys: Set[Tuple[str, str]] = set()
        if retrieved:
            # Prefer chunks matching proposed tools
            ordered_chunks = sorted(
                retrieved,
                key=lambda ch: 1 if any(t in (ch.get("allowed_actions") or []) for t in act_tools) else 0,
                reverse=True,
            )
            for ch in ordered_chunks:
                key = (str(ch.get("runbook_id") or ""), str(ch.get("section") or ""))
                if key in seen_chunk_keys:
                    continue
                seen_chunk_keys.add(key)
                filled = _fill_citation(
                    {
                        "runbook_id": ch.get("runbook_id", ""),
                        "book_name": ch.get("title", ""),
                        "section": ch.get("section", "General"),
                        "line_range": ch.get("line_range", ""),
                        "chunk_id": ch.get("chunk_id") or ch.get("chunk_label") or "",
                        "referenced_text": " ".join(str(ch.get("content") or "").split())[:240],
                    },
                    tool_hint=act_tools[0] if act_tools else "",
                )
                if filled:
                    normalized_top.append(filled)
                if len(normalized_top) >= 3:
                    break
        elif index:
            combined_text = (
                str(out.get("rationale") or "")
                + " "
                + " ".join(str(a.get("rationale") or "") for a in actions if isinstance(a, dict))
            ).lower()
            matched_id = ""
            for item in index:
                if not isinstance(item, dict):
                    continue
                i_id = str(item.get("id") or "").strip()
                i_title = str(item.get("title") or "").strip()
                if (i_id and i_id.lower() in combined_text) or (i_title and i_title.lower() in combined_text):
                    matched_id = i_id
                    break
            resolved = resolve_runbook_section_metadata(
                runbook_id=matched_id,
                namespace=namespace,
                index=index,
                tool_name=act_tools[0] if act_tools else "",
            )
            if resolved:
                filled = _fill_citation(resolved, tool_hint=act_tools[0] if act_tools else "")
                if filled:
                    normalized_top.append(filled)

    # Deduplicate normalized_top while preserving order
    deduped_top: List[Dict[str, Any]] = []
    seen_cit_keys: Set[Tuple[str, str, str, str, str]] = set()
    for c in normalized_top:
        ck = (
            c.get("runbook_id", ""),
            c.get("book_name", ""),
            c.get("section", ""),
            c.get("line_range", ""),
            c.get("chunk_id", ""),
        )
        if ck not in seen_cit_keys:
            seen_cit_keys.add(ck)
            deduped_top.append(c)

    out["runbook_citations"] = deduped_top

    # Propagate citations and structured multi-line reference into action rationales
    for act in actions:
        if not isinstance(act, dict):
            continue
        if not act.get("runbook_citations") and deduped_top:
            act["runbook_citations"] = list(deduped_top)
        act_cits = act.get("runbook_citations") or []
        if act_cits:
            primary_cit = act_cits[0]
            ref_inline = _format_citation_inline_ref(primary_cit)
            rat = str(act.get("rationale") or "").strip()
            l_range = primary_cit.get("line_range", "")
            if "[Runbook Reference]" not in rat and "[Runbook Ref:" not in rat and (not l_range or l_range not in rat):
                act["rationale"] = f"{rat}\n\n{ref_inline}".strip() if rat else ref_inline

    out["actions"] = actions
    if not str(out.get("rationale") or "").strip() and actions and isinstance(actions[0], dict):
        out["rationale"] = actions[0].get("rationale", "")
    elif str(out.get("rationale") or "").strip() and deduped_top:
        top_rat = str(out["rationale"]).strip()
        primary_cit = deduped_top[0]
        l_range = primary_cit.get("line_range", "")
        if "[Runbook Reference]" not in top_rat and "[Runbook Ref:" not in top_rat and (not l_range or l_range not in top_rat):
            out["rationale"] = f"{top_rat}\n\n{_format_citation_inline_ref(primary_cit)}".strip()

    return out


def create_remediation_graph(
    llm: Optional[Any] = None,
    qdrant_fn: Optional[Callable[..., Any]] = None,
    write_executor_fn: Optional[Callable[..., Any]] = None,
    convergence_fn: Optional[Callable[..., Any]] = None,
    checkpointer: Optional[Any] = None,
    redis_client: Optional[Any] = None,
    transition_fn: Optional[Callable[..., Any]] = None,
):
    """Construct and compile the LangGraph Phase 2 Remediation Graph with ReAct retrieval."""
    config_mgr = ConfigManager()

    # Node 1: formulate_fix (ReAct retrieval loop over catalog + rules)
    async def formulate_fix_node(state: Phase2GraphState) -> Phase2GraphState:
        # If action_proposal is already provided and approved (e.g. resumed execution or pre-formulated state), pass forward
        if state.get("action_proposal") and state.get("approval_status") == "approved":
            return {
                "action_proposal": state.get("action_proposal"),
                "action_id": state.get("action_id"),
                "approval_status": "approved",
                "runbook_index": state.get("runbook_index"),
                "retrieved_runbooks": state.get("retrieved_runbooks", []),
            }

        incident_id = state.get("incident_id", "unknown")
        severity = state.get("severity", "P2")
        finding = state.get("merged_finding", {})
        prior_attempts = state.get("prior_attempts", [])
        steering = state.get("steering_instructions")
        retrieved_runbooks = list(state.get("retrieved_runbooks", []))

        # 1. Load runbooks index (TOC) dynamically for incident namespace & hot-inject operational rules
        index = state.get("runbook_index")
        if index is None:
            index = load_runbooks_index(namespace=state.get("affected_namespace") or None)
        rules_text = extract_operational_rules(index)

        qdrant_cfg = getattr(getattr(config_mgr, "system", None), "qdrant", None)
        raw_mode = getattr(qdrant_cfg, "catalog_mode", "auto")
        catalog_mode = (
            raw_mode.strip().lower()
            if isinstance(raw_mode, str) and raw_mode.strip().lower() in ("auto", "direct", "two_step")
            else "auto"
        )
        raw_max_inline = getattr(qdrant_cfg, "catalog_inline_max_entries", 15)
        max_inline = (
            raw_max_inline
            if isinstance(raw_max_inline, int) and not isinstance(raw_max_inline, bool) and raw_max_inline > 0
            else 15
        )
        use_two_step = should_use_two_step_catalog(index, mode=catalog_mode, max_inline=max_inline)
        catalog_text = "" if use_two_step else format_catalog_prompt(index)

        base_system_prompt = load_system_prompt()
        if rules_text:
            system_prompt = (
                f"{base_system_prompt}\n\n"
                f"<operational_rules>\n{rules_text}\n</operational_rules>"
            )
        else:
            system_prompt = base_system_prompt

        # Resolve max retrieval steps from config / env with fallback to 8
        env_limit = os.getenv("AEGIS_REMEDIATION_MAX_RETRIEVAL_TURNS")
        if env_limit and env_limit.isdigit():
            max_retrieval_steps = int(env_limit)
        else:
            agents_map = getattr(getattr(config_mgr, "agents", None), "agents", None) or {}
            remediation_cfg = agents_map.get("remediation") if isinstance(agents_map, dict) else None
            max_retrieval_steps = (
                remediation_cfg.max_tool_calls
                or remediation_cfg.max_iter
                or 8
            ) if remediation_cfg else 8

        aff_svc = state.get("affected_service", "unknown")
        aff_ns = state.get("affected_namespace", "")

        # Check if re-planning (mutation detected or retry) and adjust/reset ReAct turn counter
        is_replan = bool(
            state.get("pre_exec_replan_count", 0) > 0
            or state.get("outcome") == "pre_exec_replan"
            or state.get("mutation_delta")
        )
        if is_replan:
            logger.info("Incident %s re-planning detected; resetting ReAct retrieval turn budget", incident_id)
            react_turns_used = 0
            budget_label = f"Max allowed retrieval steps: {max_retrieval_steps} (Turn budget reset for re-planning after pre-execution mutation detection)"
        else:
            react_turns_used = state.get("react_turns_used", 0)
            budget_label = f"Max allowed retrieval steps: {max_retrieval_steps}"

        def _build_ranked_agent_items(finding_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
            ranked_raw = finding_dict.get("ranked_findings")
            items: List[Tuple[str, Dict[str, Any]]] = []
            if isinstance(ranked_raw, list) and ranked_raw:
                for entry in ranked_raw:
                    if isinstance(entry, dict):
                        items.append((str(entry.get("agent_role", "unknown")), entry))
            elif isinstance(finding_dict.get("findings_by_agent"), dict):
                raw_map = finding_dict["findings_by_agent"]
                items = sorted(
                    ((str(k), v) for k, v in raw_map.items() if isinstance(v, dict)),
                    key=lambda kv: (
                        0 if ("[ungrounded" in str(kv[1].get("hypothesis", "")).lower() or "no tools called" in str(kv[1].get("hypothesis", "")).lower()) else (2 if kv[1].get("evidence") else 1),
                        float(kv[1].get("confidence") or 0.0),
                    ),
                    reverse=True,
                )

            max_per_agent = 3
            max_total_to_remediation = 5
            summaries: List[Dict[str, Any]] = []
            role_counts: Dict[str, int] = {}
            stratified_items: List[Tuple[str, Dict[str, Any]]] = []
            used_indices: Set[int] = set()

            # Pass 1: guarantee at least 1 finding per specialist role (regardless of confidence)
            for idx_raw, entry in enumerate(items):
                role_name = entry[0]
                if role_counts.get(role_name, 0) == 0 and len(stratified_items) < max_total_to_remediation:
                    role_counts[role_name] = 1
                    used_indices.add(idx_raw)
                    stratified_items.append(entry)

            # Pass 2: fill remaining slots up to max 3 per agent and max 5 total to Remediation
            for idx_raw, entry in enumerate(items):
                if len(stratified_items) >= max_total_to_remediation:
                    break
                if idx_raw in used_indices:
                    continue
                role_name = entry[0]
                if role_counts.get(role_name, 0) >= max_per_agent:
                    continue
                role_counts[role_name] = role_counts.get(role_name, 0) + 1
                used_indices.add(idx_raw)
                stratified_items.append(entry)

            for idx, (role_name, agent_data) in enumerate(stratified_items, start=1):
                hyp = str(agent_data.get("hypothesis", "")).strip()
                agent_ev = agent_data.get("evidence") or []
                agent_signals = agent_data.get("verbatim_signals") or {}
                exec_status = str(agent_signals.get("tool_execution_status", "")).lower()
                zero_tools = (
                    not agent_ev
                    and (
                        "[ungrounded" in hyp.lower()
                        or "no tools called" in hyp.lower()
                        or "no tools called" in exec_status
                    )
                )
                # Cap evidence per agent at 3, prioritizing diverse source_tool names
                selected_raw_ev: List[Dict[str, Any]] = []
                used_tools: Set[str] = set()
                overflow_ev: List[Dict[str, Any]] = []
                for ev in agent_ev:
                    if not isinstance(ev, dict):
                        continue
                    t_name = str(ev.get("source_tool", ""))
                    if t_name not in used_tools and len(selected_raw_ev) < max_per_agent:
                        used_tools.add(t_name)
                        selected_raw_ev.append(ev)
                    else:
                        overflow_ev.append(ev)
                for ev in overflow_ev:
                    if len(selected_raw_ev) >= max_per_agent:
                        break
                    selected_raw_ev.append(ev)

                formatted_ev = [
                    {
                        "source_tool": ev.get("source_tool", ""),
                        "query_or_cmd": str(ev.get("query_or_cmd", ""))[:200],
                        "raw_output": str(ev.get("raw_output", ""))[:800],
                    }
                    for ev in selected_raw_ev
                ]
                summaries.append({
                    "rank": idx,
                    "agent": role_name,
                    "status": "no tools called, 0 evidence" if zero_tools else f"grounded ({len(formatted_ev)} evidence items)",
                    "confidence": agent_data.get("confidence"),
                    "hypothesis": hyp if hyp else "no tools called, 0 evidence",
                    "evidence": formatted_ev,
                    "verbatim_signals": agent_signals,
                    "infra_flags": agent_data.get("infra_flags") or {},
                    "recommended_actions": (agent_data.get("recommended_actions") or [])[:3],
                })
            return summaries

        agent_summaries = _build_ranked_agent_items(finding)

        # Stratified evidence pool sent to Remediation: max 3 per agent, max 5 total
        # (MergedFinding in state/Redis retains all evidence undropped)
        evidence_snippets: List[Dict[str, Any]] = []
        seen_snippet_keys: Set[Tuple[str, str]] = set()
        per_agent_ev_counts: Dict[str, int] = {}
        # Round-robin across agents first (up to 3 per agent, 5 total)
        for ev_slot in range(3):
            if len(evidence_snippets) >= 5:
                break
            for summary_item in agent_summaries:
                if len(evidence_snippets) >= 5:
                    break
                agent_name = str(summary_item.get("agent", "unknown"))
                if per_agent_ev_counts.get(agent_name, 0) >= 3:
                    continue
                ev_list = summary_item.get("evidence") or []
                if ev_slot < len(ev_list):
                    ev = ev_list[ev_slot]
                    ev_key = (str(ev.get("source_tool", "")), str(ev.get("query_or_cmd", "")))
                    if ev_key not in seen_snippet_keys:
                        seen_snippet_keys.add(ev_key)
                        per_agent_ev_counts[agent_name] = per_agent_ev_counts.get(agent_name, 0) + 1
                        evidence_snippets.append(ev)
        # Backfill up to 5 total from flat all_evidence when callers omit findings_by_agent/ranked_findings
        if len(evidence_snippets) < 5:
            all_evidence = finding.get("all_evidence") or []
            if isinstance(all_evidence, list):
                for ev in all_evidence:
                    if len(evidence_snippets) >= 5:
                        break
                    if not isinstance(ev, dict):
                        continue
                    ev_key = (str(ev.get("source_tool", "")), str(ev.get("query_or_cmd", "")))
                    if ev_key not in seen_snippet_keys:
                        seen_snippet_keys.add(ev_key)
                        evidence_snippets.append({
                            "source_tool": ev.get("source_tool", ""),
                            "query_or_cmd": str(ev.get("query_or_cmd", ""))[:200],
                            "raw_output": str(ev.get("raw_output", ""))[:800],
                        })

        recent_recurrences = (finding.get("verbatim_signals") or {}).get("recent_recurrences") or []
        if not recent_recurrences and redis_client is not None:
            try:
                import inspect as _inspect
                _raw_ag = redis_client.get(f"incident:{incident_id}:alert_group")
                raw_ag = await _raw_ag if _inspect.isawaitable(_raw_ag) else _raw_ag
                if raw_ag:
                    if isinstance(raw_ag, (bytes, bytearray)):
                        raw_ag = raw_ag.decode("utf-8")
                    ag_dict = json.loads(raw_ag)
                    if ag_dict.get("chronic_recurrence") and ag_dict.get("recent_recurrences"):
                        recent_recurrences = ag_dict["recent_recurrences"]
            except Exception:
                pass

        safe_recurrences: List[Dict[str, Any]] = []
        allowed_rec_keys = (
            "incident_id", "alertname", "service", "state",
            "outcome", "hypothesis", "last_action_tool", "action_rationale", "timestamp",
        )
        for rec in recent_recurrences:
            if isinstance(rec, dict):
                safe_item = {k: rec[k] for k in allowed_rec_keys if k in rec}
                if "hypothesis" in safe_item:
                    safe_item["hypothesis"] = str(safe_item["hypothesis"])
                if "action_rationale" in safe_item:
                    safe_item["action_rationale"] = str(safe_item["action_rationale"])
                safe_recurrences.append(safe_item)

        context_blocks = [
            (
                f"<incident_summary>\nIncident: {incident_id}\nSeverity: {severity}\n"
                f"Affected Service: {aff_svc}\nAffected Namespace: {aff_ns}\n"
                f"Top-Ranked Hypothesis (Advisory): {finding.get('primary_hypothesis', '')}\n</incident_summary>"
            ),
            f"<verbatim_evidence>\n{json.dumps(finding.get('verbatim_signals', {}), indent=2)}\n</verbatim_evidence>",
        ]
        if agent_summaries:
            context_blocks.append(
                f"<investigator_findings>\n"
                f"# Ranked Candidate Hypotheses (evaluate ALL findings and raw tool evidence freely; do NOT lock solely onto Rank #1)\n"
                f"{json.dumps(agent_summaries, indent=2)}\n</investigator_findings>"
            )
        if evidence_snippets:
            context_blocks.append(
                f"<phase1_evidence>\n{json.dumps(evidence_snippets, indent=2)}\n</phase1_evidence>"
            )
        if safe_recurrences:
            context_blocks.append(
                f"<recent_recurrence_window>\n"
                f"# Guard: bounded prior problem hypothesis + remedy/rationale + outcome (verify live evidence first; see why past fixes were tried and avoid repeating failed actions)\n"
                f"{json.dumps(safe_recurrences, indent=2)}\n</recent_recurrence_window>"
            )
        if use_two_step:
            catalog_count = sum(1 for item in (index or []) if isinstance(item, dict))
            catalog_block = (
                f"<runbook_catalog_mode>\n"
                f"Two-step runbook catalog retrieval is active "
                f"(mode={catalog_mode}, indexed_entries={catalog_count}, inline_threshold={max_inline}).\n"
                f"1. Call `search_runbook_catalog(query=..., symptoms=..., service=..., limit=5)` first to discover matching runbook IDs, sections, and chunk IDs.\n"
                f"2. Call `qdrant_search(id=\"<runbook_id>\")` or `qdrant_search(query=...)` to read exact remediation steps and line ranges.\n"
                f"3. Formulate your final ActionProposal JSON once you have verified runbook context.\n"
                f"</runbook_catalog_mode>"
            )
        else:
            catalog_block = f"<runbook_catalog>\n{catalog_text}\n</runbook_catalog>"

        context_blocks.extend([
            catalog_block,
            f"<retrieval_budget>\n{budget_label}\n</retrieval_budget>",
            f"<prior_attempts>\n{json.dumps(prior_attempts, indent=2)}\n</prior_attempts>",
        ])

        mutation_delta = state.get("mutation_delta")
        if not mutation_delta and redis_client:
            try:
                import inspect
                _res = redis_client.get(f"incident:{incident_id}:mutation_delta")
                raw_delta = await _res if inspect.isawaitable(_res) else _res
                if raw_delta:
                    mutation_delta = raw_delta.decode("utf-8") if isinstance(raw_delta, (bytes, bytearray)) else str(raw_delta)
            except Exception:
                pass

        if mutation_delta:
            context_blocks.append(
                f"<live_mutation_delta>\n{mutation_delta}\n"
                f"NOTICE: The cluster state evolved while awaiting approval. You MUST prioritize remediating the active live condition over the stale initial hypothesis.\n</live_mutation_delta>"
            )

        if aff_ns:
            context_blocks.append(
                f"<namespace_constraint>\nAll action parameters must strictly target the incident's affected namespace: '{aff_ns}'. Never target external namespaces.\n</namespace_constraint>"
            )
        if retrieved_runbooks:
            context_blocks.append(f"<retrieved_runbooks>\n{json.dumps(retrieved_runbooks, indent=2)}\n</retrieved_runbooks>")
        if steering:
            context_blocks.append(f"<operator_steering>\n{steering}\n</operator_steering>")

        user_content = "\n\n".join(context_blocks) + "\n\nFormulate the ActionProposal JSON now:"

        active_llm = llm or build_llm("remediation")
        proposal_dict: Optional[Dict[str, Any]] = None

        messages: List[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

        def _attach_budget_status(tool_res: Any, step_num: int, remaining_steps: int) -> Dict[str, Any]:
            base = dict(tool_res) if isinstance(tool_res, dict) else {"results": tool_res}
            base["_budget_status"] = f"Step {step_num} of {max_retrieval_steps} completed ({remaining_steps} remaining)"
            if remaining_steps == 1:
                base["_budget_notice"] = (
                    "WARNING: Only 1 retrieval step remaining. Prepare your final ActionProposal JSON."
                )
            elif remaining_steps == 0:
                base["_budget_notice"] = (
                    "CRITICAL: Final retrieval step reached. You must output the ActionProposal JSON now."
                )
            return base

        try:
            # ReAct retrieval loop: dynamically bounded by configured retrieval steps
            for step_idx in range(max_retrieval_steps):
                step_num = step_idx + 1
                remaining_steps = max_retrieval_steps - step_num

                response = None
                for llm_attempt in range(3):
                    try:
                        response = await active_llm.ainvoke(messages)
                        break
                    except Exception as llm_err:
                        if llm_attempt == 2:
                            raise
                        logger.warning(
                            f"Transient LLM call error on attempt {llm_attempt + 1}/3: {llm_err}. Retrying in 2s..."
                        )
                        import asyncio
                        await asyncio.sleep(2)

                if isinstance(response, ActionProposal):
                    proposal_dict = response.model_dump(mode="json")
                    break

                raw_tool_calls = getattr(response, "tool_calls", None)
                if isinstance(raw_tool_calls, list) and len(raw_tool_calls) > 0:
                    messages.append(response)
                    for tc in raw_tool_calls:
                        tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                        tc_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "tool-call")

                        if tc_name in ("qdrant_search", "search_runbooks"):
                            # Resolve target collection dynamically from incident namespace if available
                            target_collection = None
                            if aff_ns:
                                clean_ns = aff_ns.lower().replace("-", "_")
                                target_collection = f"aegis_runbooks_{clean_ns}"

                            q_kwargs = dict(tc_args) if isinstance(tc_args, dict) else {"query": str(tc_args)}
                            if target_collection and ("collection" not in q_kwargs or q_kwargs.get("collection") in (None, "", "aegis_runbooks")):
                                q_kwargs["collection"] = target_collection

                            if qdrant_fn:
                                tool_res = await qdrant_fn(**q_kwargs)
                            else:
                                tool_res = await qdrant_search(**q_kwargs)

                            if isinstance(tool_res, dict):
                                retrieved_runbooks.extend(tool_res.get("results", []))
                            elif isinstance(tool_res, list):
                                retrieved_runbooks.extend(tool_res)

                            tool_msg_content = _attach_budget_status(tool_res, step_num, remaining_steps)
                            messages.append(
                                ToolMessage(
                                    content=json.dumps(tool_msg_content, default=str),
                                    tool_call_id=tc_id,
                                    name=tc_name,
                                )
                            )
                        elif tc_name in ("search_runbook_catalog", "runbook_catalog_search"):
                            cat_kwargs = dict(tc_args) if isinstance(tc_args, dict) else {"query": str(tc_args)}
                            try:
                                raw_cat_limit = int(cat_kwargs.get("limit", 5) or 5)
                            except (TypeError, ValueError):
                                raw_cat_limit = 5
                            cat_res = search_runbook_catalog(
                                index,
                                query=str(cat_kwargs.get("query", "") or ""),
                                symptoms=str(cat_kwargs.get("symptoms", "") or ""),
                                service=str(cat_kwargs.get("service", "") or ""),
                                limit=raw_cat_limit,
                            )
                            cat_msg_content = _attach_budget_status(cat_res, step_num, remaining_steps)
                            messages.append(
                                ToolMessage(
                                    content=json.dumps(cat_msg_content, default=str),
                                    tool_call_id=tc_id,
                                    name=tc_name,
                                )
                            )
                        elif tc_name in ("k8s_get_pod_diagnostics", "kubernetes_k8s_get_pod_diagnostics"):
                            from services.mcp.tools.kubernetes import k8s_get_pod_diagnostics
                            diag_kwargs = dict(tc_args) if isinstance(tc_args, dict) else {}
                            if "namespace" not in diag_kwargs and aff_ns:
                                diag_kwargs["namespace"] = aff_ns
                            if "service_or_pod" not in diag_kwargs and aff_svc:
                                diag_kwargs["service_or_pod"] = aff_svc
                            try:
                                diag_res = await k8s_get_pod_diagnostics(**diag_kwargs)
                            except Exception as d_exc:
                                diag_res = {"error": f"Failed to execute pod diagnostics: {d_exc}", "found": False}
                            messages.append(
                                ToolMessage(
                                    content=json.dumps(diag_res, default=str),
                                    tool_call_id=tc_id,
                                    name=tc_name,
                                )
                            )

                    # Poll for mid-flight operator intervention or standby between ReAct iterations
                    if redis_client is not None:
                        try:
                            # 1. Standby check
                            raw_st = redis_client.get(f"incident:{incident_id}:state")
                            if raw_st:
                                if isinstance(raw_st, (bytes, bytearray)):
                                    raw_st = raw_st.decode("utf-8")
                                if json.loads(raw_st).get("state") == "STANDBY":
                                    logger.info(f"Incident {incident_id} placed in STANDBY; pausing ReAct retrieval.")
                                    return {"outcome": "standby", "runbook_index": index, "retrieved_runbooks": retrieved_runbooks}

                            # 2. Dynamic mid-flight steering check
                            raw_steer = redis_client.get(f"incident:{incident_id}:steering")
                            if raw_steer:
                                if isinstance(raw_steer, (bytes, bytearray)):
                                    raw_steer = raw_steer.decode("utf-8")
                                try:
                                    steer_obj = json.loads(raw_steer)
                                    steer_text = steer_obj.get("instruction", raw_steer)
                                except Exception:
                                    steer_text = raw_steer
                                if steer_text and steer_text != steering:
                                    messages.append(HumanMessage(content=f"[OPERATOR MID-FLIGHT GUIDANCE]: {steer_text}"))
                                    steering = steer_text
                                    logger.info(f"Injected mid-flight operator guidance into step {step_num}")
                        except Exception as poll_exc:
                            logger.warning(f"Error polling mid-flight interventions for {incident_id}: {poll_exc}")
                else:
                    # Direct response: parse ActionProposal
                    if hasattr(response, "content"):
                        if isinstance(response.content, list):
                            raw_text = "".join(
                                part.get("text", "") if isinstance(part, dict) else str(part)
                                for part in response.content
                            )
                        else:
                            raw_text = str(response.content)
                    else:
                        raw_text = str(response)

                    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
                    if match:
                        clean_json = match.group(1)
                    else:
                        brace_match = re.search(r"(\{.*\})", raw_text, re.DOTALL)
                        clean_json = brace_match.group(1) if brace_match else raw_text.strip()

                    parsed = json.loads(clean_json)
                    if isinstance(parsed, dict) and "incident_id" not in parsed:
                        parsed["incident_id"] = incident_id
                    proposal_obj = ActionProposal.model_validate(parsed)
                    proposal_dict = proposal_obj.model_dump(mode="json")
                    break

        except Exception as e:
            logger.error(f"Failed to formulate action proposal via LLM: {e}")
            return {
                "outcome": "low_confidence",
                "error": f"LLM formulation error: {str(e)}",
                "runbook_index": index,
                "retrieved_runbooks": retrieved_runbooks,
            }

        if proposal_dict is not None:
            try:
                proposal_dict = _enrich_proposal_citations(
                    proposal_dict,
                    retrieved_runbooks=retrieved_runbooks,
                    index=index,
                    namespace=aff_ns,
                )
            except Exception as cit_err:
                logger.warning("Failed to enrich runbook citations for %s: %s", incident_id, cit_err)

        return {
            "action_proposal": proposal_dict,
            "action_id": state.get("action_id"),
            "approval_status": state.get("approval_status"),
            "runbook_index": index,
            "retrieved_runbooks": retrieved_runbooks,
            "react_turns_used": step_num,
        }

    # Node 2: check_confidence
    def check_confidence_node(state: Phase2GraphState) -> Phase2GraphState:
        proposal = state.get("action_proposal")
        if not proposal:
            return {"outcome": "low_confidence"}

        conf = float(proposal.get("confidence", 0.0))
        sev = state.get("severity", "P2")
        threshold = config_mgr.sag.confidence_thresholds.get(sev, 0.80)

        if conf < threshold:
            logger.info(f"Proposal confidence {conf:.2f} below threshold {threshold:.2f} for {sev}")
            return {"outcome": "low_confidence"}

        return {"outcome": "confidence_met"}

    # Node 3: propose_action
    def propose_action_node(state: Phase2GraphState) -> Phase2GraphState:
        proposal = state.get("action_proposal", {})
        actions = proposal.get("actions", [])
        if not actions:
            return {"pre_approved": False, "action_id": None, "approval_status": None, "outcome": "action_proposed"}

        # Check if all actions are pre_approved in sag_policies
        all_pre_approved = True
        for act in actions:
            tool_name = act.get("tool_name", "")
            policy = config_mgr.sag.get_tool_policy(tool_name)
            if not policy.pre_approved:
                all_pre_approved = False
                break

        incident_id = state.get("incident_id", "unknown")
        act_id = state.get("action_id")
        if not act_id and not state.get("approval_status"):
            import uuid
            act_id = f"act-{incident_id}-{uuid.uuid4().hex[:8]}"

        if all_pre_approved:
            return {
                "pre_approved": True,
                "action_id": act_id,
                "approval_status": "approved",
                "outcome": "action_proposed",
            }

        return {
            "pre_approved": False,
            "action_id": act_id,
            "approval_status": state.get("approval_status"),
            "outcome": "action_proposed",
        }

    # Node 4: request_approval
    def request_approval_node(state: Phase2GraphState) -> Phase2GraphState:
        # If approval_status was already provided (resumed or simulated)
        status = state.get("approval_status")
        act_id = state.get("action_id")
        if not status:
            if checkpointer is not None:
                try:
                    decision = interrupt({
                        "type": "approval_required",
                        "incident_id": state.get("incident_id"),
                        "action_id": act_id,
                        "proposal": state.get("action_proposal"),
                    })
                    if isinstance(decision, dict):
                        status = decision.get("decision", "denied")
                        state["steering_instructions"] = decision.get("steering")
                    elif isinstance(decision, str):
                        status = decision
                except Exception:
                    return {"outcome": "action_proposed", "action_id": act_id, "approval_status": None}
            else:
                return {"outcome": "action_proposed", "action_id": act_id, "approval_status": None}

        state["approval_status"] = status
        if status == "approved":
            return {"approval_status": "approved", "action_id": act_id, "outcome": "approved"}
        elif status == "steer":
            return {"approval_status": "steer", "outcome": "steered"}
        elif status == "denied":
            return {"approval_status": "denied", "action_id": act_id, "outcome": "denied"}
        else:
            logger.info("Action '%s' approval_status is '%s'; awaiting decision (not denied)", act_id, status)
            return {"outcome": "action_proposed", "action_id": act_id, "approval_status": None}

    # Node 5: execute_action
    async def execute_action_node(state: Phase2GraphState) -> Phase2GraphState:
        incident_id = state.get("incident_id")
        action_id = state.get("action_id")
        if not action_id or not incident_id:
            logger.error("execute_action_node failed closed: missing incident_id (%s) or action_id (%s)", incident_id, action_id)
            return {
                "outcome": "execution_failed",
                "error": "missing incident_id or action_id for SAG authorization",
            }

        proposal = state.get("action_proposal", {})
        actions = proposal.get("actions", [])
        results: List[Dict[str, Any]] = []

        from shared.harness.sag_guard import SAGGuard
        r_client = redis_client
        if r_client is None:
            import redis
            try:
                r_url = config_mgr.system.redis.url
            except Exception:
                r_url = "redis://localhost:6379"
            r_client = redis.from_url(r_url, decode_responses=True)

        for act in actions:
            if r_client is not None:
                try:
                    import inspect
                    _res = r_client.get(f"incident:{incident_id}:state")
                    raw_st = await _res if inspect.isawaitable(_res) else _res
                    if raw_st:
                        if isinstance(raw_st, (bytes, bytearray)):
                            raw_st = raw_st.decode("utf-8")
                        if json.loads(raw_st).get("state") == "STANDBY":
                            logger.info(f"Incident {incident_id} entered STANDBY; pausing action execution.")
                            return {"execution_results": results, "outcome": "standby"}
                except Exception as st_exc:
                    logger.warning(f"Error checking standby before action execution: {st_exc}")

            tool_name = act.get("tool_name", "")
            params = dict(act.get("parameters", {}))

            # Resolve affected_namespace from state or Redis incident state
            incident_ns = state.get("affected_namespace")
            if not incident_ns and r_client:
                try:
                    import inspect
                    _res = r_client.get(f"incident:{incident_id}:state")
                    raw_st = await _res if inspect.isawaitable(_res) else _res
                    if raw_st:
                        if isinstance(raw_st, (bytes, bytearray)):
                            raw_st = raw_st.decode("utf-8")
                        incident_ns = json.loads(raw_st).get("affected_namespace")
                except Exception as ns_exc:
                    logger.warning(f"Error fetching affected_namespace for {incident_id}: {ns_exc}")

            # Server-supplied incident_id and action_id override any LLM params
            safe_params = {**params, "incident_id": incident_id, "action_id": action_id}
            if incident_ns and "namespace" in params:
                safe_params["namespace"] = incident_ns

            # Resolve target service & namespace for pre-execution gate
            tgt_service = safe_params.get("deployment") or safe_params.get("app") or safe_params.get("pod_name") or state.get("affected_service")
            tgt_ns = safe_params.get("namespace") or incident_ns or state.get("affected_namespace") or "default"
            merged_f = state.get("merged_finding") or {}
            active_hypothesis = (
                state.get("hypothesis")
                or (merged_f.get("primary_hypothesis") if isinstance(merged_f, dict) else "")
                or act.get("rationale", "")
            )

            # Pre-Execution Gate: Check live substrate before administering remediation
            if tgt_service and str(tgt_service) not in ("unknown", "cluster") and not tool_name.startswith("toggle_feature"):
                diag_res = {"found": False}
                try:
                    mcp = resolve_mcp_client_for_tool("k8s_get_pod_diagnostics")
                    raw_mcp = await mcp.call_tool("k8s_get_pod_diagnostics", {"namespace": tgt_ns, "service_or_pod": str(tgt_service)})
                    if isinstance(raw_mcp, dict) and "content" in raw_mcp and raw_mcp["content"]:
                        txt = raw_mcp["content"][0].get("text", "{}")
                        try:
                            diag_res = json.loads(txt)
                        except Exception:
                            diag_res = raw_mcp
                    elif isinstance(raw_mcp, dict):
                        diag_res = raw_mcp
                except Exception as mcp_diag_err:
                    try:
                        from services.mcp.tools.kubernetes import k8s_get_pod_diagnostics
                        diag_res = await k8s_get_pod_diagnostics(namespace=tgt_ns, service_or_pod=str(tgt_service))
                    except Exception as diag_err:
                        logger.warning(f"Pre-execution diagnostic check failed to query pod status: {diag_err}")
                        diag_res = {"found": False, "error": str(diag_err)}

                replan_count = state.get("pre_exec_replan_count", 0)
                gate_eval = evaluate_pre_execution_gate(
                    tool_name=tool_name,
                    tool_params=safe_params,
                    diag=diag_res,
                    replan_count=replan_count,
                    max_replans=1,
                    hypothesis=str(active_hypothesis),
                )
                verdict = gate_eval.get("verdict")
                v_reason = gate_eval.get("reason", "")
                v_sig = gate_eval.get("signature", {})

                # Record Pre-Execution Diagnostics snapshot in Central Ledger
                if r_client is not None:
                    try:
                        diag_entry = LedgerEntry(
                            entry_id=f"evt-{datetime.now(timezone.utc).timestamp()}-pre_exec_diagnostics",
                            incident_id=incident_id,
                            state=IncidentState.EXECUTING,
                            event_type="pre_execution_diagnostics",
                            source="phase2_worker",
                            details={
                                "tool_name": tool_name,
                                "target_service": tgt_service,
                                "target_namespace": tgt_ns,
                                "failure_class": v_sig.get("failure_class"),
                                "gate_verdict": verdict,
                                "reason": v_reason,
                                "pod_diagnostics_summary": {
                                    "pod_count": diag_res.get("pod_count", 0),
                                    "critical_issues": diag_res.get("critical_issues", []),
                                    "recent_events_count": len(diag_res.get("recent_events", [])),
                                },
                            },
                        )
                        append_ledger_entry(r_client, incident_id, diag_entry)
                    except Exception as led_err:
                        logger.warning(f"Failed to record pre-execution diagnostics to ledger: {led_err}")

                # CASE 1: RESOLVED_SKIP (Transient fault subsided; pods healthy; skip disruptive tool)
                if verdict in (PreExecVerdict.RESOLVED_SKIP, "SELF_RESOLVED"):
                    logger.info(f"Incident {incident_id} pre-execution check: {v_reason}. Skipping tool '{tool_name}'.")
                    if r_client is not None:
                        try:
                            sr_entry = LedgerEntry(
                                entry_id=f"evt-{datetime.now(timezone.utc).timestamp()}-pre_exec_self_resolved",
                                incident_id=incident_id,
                                state=IncidentState.EXECUTING,
                                event_type="pre_execution_self_resolved",
                                source="phase2_worker",
                                details={
                                    "tool_withheld": tool_name,
                                    "reason": v_reason,
                                    "course_correction": "Skipped write action; proceeding directly to convergence monitoring.",
                                },
                            )
                            append_ledger_entry(r_client, incident_id, sr_entry)
                        except Exception:
                            pass
                    results.append({
                        "tool": tool_name,
                        "status": "skipped",
                        "verdict": verdict,
                        "reason": v_reason,
                    })
                    continue

                # CASE 2: MUTATION_DETECTED_REPLAN (Condition evolved into incompatible failure class)
                elif verdict == PreExecVerdict.MUTATION_DETECTED_REPLAN:
                    logger.warning(
                        f"Incident {incident_id} pre-execution check: Condition mutated! {v_reason}. Aborting '{tool_name}' and triggering re-plan."
                    )
                    mutation_delta_text = (
                        f"Pre-execution check aborted '{tool_name}': {v_reason} "
                        f"Live failure class: {v_sig.get('failure_class')} ({v_sig.get('detail')}). "
                        f"Critical issues: {json.dumps(diag_res.get('critical_issues', []))}."
                    )

                    prior_attempts = list(state.get("prior_attempts", []))
                    prior_attempts.append({
                        "attempt": len(prior_attempts) + 1,
                        "action": act,
                        "outcome": "aborted_pre_execution_mutation",
                        "mutation_delta": mutation_delta_text,
                    })

                    if r_client is not None:
                        try:
                            mut_entry = LedgerEntry(
                                entry_id=f"evt-{datetime.now(timezone.utc).timestamp()}-pre_exec_mutation_detected",
                                incident_id=incident_id,
                                state=IncidentState.EXECUTING,
                                event_type="pre_execution_mutation_detected",
                                source="phase2_worker",
                                details={
                                    "aborted_tool": tool_name,
                                    "failure_class": v_sig.get("failure_class"),
                                    "reason": v_reason,
                                    "mutation_delta": mutation_delta_text,
                                    "course_correction": "Aborted incompatible action; transitioning to REMEDIATION to re-plan.",
                                },
                            )
                            append_ledger_entry(r_client, incident_id, mut_entry)
                            import inspect
                            _s1 = r_client.set(f"incident:{incident_id}:mutation_delta", mutation_delta_text)
                            if inspect.isawaitable(_s1):
                                await _s1
                            _s2 = r_client.set(f"incident:{incident_id}:prior_attempts", json.dumps(prior_attempts, default=str))
                            if inspect.isawaitable(_s2):
                                await _s2
                        except Exception as p_err:
                            logger.warning(f"Failed to record mutation to Redis: {p_err}")

                    if transition_fn:
                        try:
                            import inspect
                            if inspect.iscoroutinefunction(transition_fn):
                                await transition_fn(
                                    incident_id,
                                    "pre_exec_replan",
                                    metadata={
                                        "aborted_tool": tool_name,
                                        "new_failure_class": v_sig.get("failure_class"),
                                        "reason": v_reason,
                                        "mutation_delta": mutation_delta_text,
                                    },
                                )
                            else:
                                transition_fn(
                                    incident_id,
                                    "pre_exec_replan",
                                    metadata={
                                        "aborted_tool": tool_name,
                                        "new_failure_class": v_sig.get("failure_class"),
                                        "reason": v_reason,
                                        "mutation_delta": mutation_delta_text,
                                    },
                                )
                        except Exception as tr_err:
                            logger.warning(f"Failed to trigger pre_exec_replan transition: {tr_err}")

                    return {
                        "execution_results": results,
                        "outcome": "pre_exec_replan",
                        "action_proposal": None,
                        "approval_status": None,
                        "action_id": None,
                        "mutation_delta": mutation_delta_text,
                        "prior_attempts": prior_attempts,
                        "pre_exec_replan_count": replan_count + 1,
                        "react_turns_used": 0,
                    }

                # CASE 3: FAULT_CONFIRMED_PROCEED
                logger.info(f"Incident {incident_id} pre-execution check: {v_reason}. Proceeding with '{tool_name}'.")

            try:
                if write_executor_fn:
                    res = await write_executor_fn(tool_name, safe_params)
                else:
                    mcp = resolve_mcp_client_for_tool(tool_name)
                    sag_guard = SAGGuard(mcp_client=mcp, redis_client=r_client)
                    res = await sag_guard.call_tool(tool_name, safe_params, incident_id, action_id)

                # Check for FastMCP or inner tool error payload
                is_err = False
                err_detail = ""
                if isinstance(res, dict):
                    if res.get("isError") is True or res.get("success") is False:
                        is_err = True
                        err_detail = str(res.get("error") or res.get("message") or res)
                    elif "content" in res and isinstance(res["content"], list):
                        for block in res["content"]:
                            if isinstance(block, dict) and block.get("type") == "text":
                                txt = block.get("text", "")
                                try:
                                    inner = json.loads(txt)
                                    if isinstance(inner, dict) and (inner.get("isError") is True or inner.get("success") is False):
                                        is_err = True
                                        err_detail = str(inner.get("error") or inner.get("message") or inner)
                                        break
                                except Exception:
                                    pass

                if is_err:
                    logger.error(f"Execution failed for tool {tool_name}: {err_detail}")
                    results.append({"tool": tool_name, "status": "failed", "error": err_detail, "result": res})
                    return {
                        "execution_results": results,
                        "outcome": "execution_failed",
                        "error": err_detail,
                    }

                results.append({"tool": tool_name, "status": "success", "result": res})
            except Exception as e:
                logger.error(f"Execution failed for tool {tool_name}: {e}")
                results.append({"tool": tool_name, "status": "failed", "error": str(e)})
                return {
                    "execution_results": results,
                    "outcome": "execution_failed",
                    "error": str(e),
                }

        if transition_fn:
            try:
                import inspect
                if inspect.iscoroutinefunction(transition_fn):
                    await transition_fn(
                        incident_id,
                        "execution_success",
                        metadata={"execution_results": results},
                    )
                else:
                    transition_fn(
                        incident_id,
                        "execution_success",
                        metadata={"execution_results": results},
                    )
            except Exception as tr_exc:
                logger.warning(
                    "Failed to post execution_success transition for incident %s: %s",
                    incident_id,
                    tr_exc,
                )

        return {
            "execution_results": results,
            "outcome": "executed",
        }

    # Node 6: monitor_convergence
    async def monitor_convergence_node(state: Phase2GraphState) -> Phase2GraphState:
        incident_id = state.get("incident_id", "unknown")

        aff_svc = state.get("affected_service")
        aff_ns = state.get("affected_namespace")
        affected_services: List[str] = []

        def _add_svc(name: Any) -> None:
            if not name:
                return
            cleaned = str(name).strip()
            if cleaned and cleaned.lower() not in ("unknown", "cluster") and cleaned not in affected_services:
                affected_services.append(cleaned)

        _add_svc(aff_svc)
        merged_f = state.get("merged_finding") or {}
        if isinstance(merged_f, dict):
            for extra_svc in merged_f.get("affected_services") or []:
                _add_svc(extra_svc)

        proposal_actions = (state.get("action_proposal") or {}).get("actions") or []
        for act in proposal_actions:
            if not isinstance(act, dict):
                continue
            params = act.get("parameters") or {}
            t_name = str(act.get("tool_name", ""))
            if t_name.startswith("toggle_feature"):
                continue
            target_svc = params.get("deployment") or params.get("app")
            if not target_svc and params.get("pod_name"):
                target_svc = re.sub(
                    r"(?:-[a-z0-9]{6,10}-[a-z0-9]{5}|-(?=[a-z0-9]*\d)[a-z0-9]{5}|-\d+)$",
                    "",
                    str(params["pod_name"]),
                )
            _add_svc(target_svc)

        if not affected_services:
            affected_services = ["default"]

        sev = state.get("severity", "P2")
        is_stateful = any(
            kw in svc.lower()
            for svc in affected_services
            for kw in ["db", "mongo", "mysql", "redis", "valkey", "postgres", "rabbit", "queue"]
        )

        if convergence_fn:
            converged, latest_signals = await convergence_fn(incident_id, affected_services, sev)
        else:
            converged, latest_signals = await run_convergence_monitor(
                incident_id=incident_id,
                affected_services=affected_services,
                severity=sev,
                is_stateful=is_stateful,
                affected_namespace=aff_ns,
                redis_client=redis_client,
            )

        raw_meta = latest_signals.get("_meta", {}) if isinstance(latest_signals, dict) else {}
        clean_signals = (
            {k: v for k, v in latest_signals.items() if k != "_meta"}
            if isinstance(latest_signals, dict)
            else {}
        )
        conv_payload = {
            "converged": bool(converged),
            "monitored_services": raw_meta.get("monitored_services") or affected_services,
            "workload_mode": raw_meta.get("workload_mode") or ("stateful" if is_stateful else "stateless"),
            "bake_time_seconds": raw_meta.get("bake_time_seconds"),
            "window_seconds": raw_meta.get("window_seconds"),
            "poll_interval_seconds": raw_meta.get("poll_interval_seconds"),
            "clean_samples": raw_meta.get("clean_samples"),
            "target_clean_samples": raw_meta.get("target_clean_samples"),
            "slo_thresholds": raw_meta.get("slo_thresholds") or {},
            "signals": clean_signals,
            "pod_checks": raw_meta.get("pod_checks") or [],
        }

        if converged:
            return {"outcome": "signals_converged", "convergence_result": conv_payload}
        else:
            return {
                "outcome": "signals_not_converged",
                "signals_at_timeout": clean_signals,
                "convergence_result": conv_payload,
            }

    # Node 7: escalate (terminal error node)
    def escalate_node(state: Phase2GraphState) -> Phase2GraphState:
        current_outcome = state.get("outcome", "escalated")
        return {"outcome": current_outcome if current_outcome in ("denied", "low_confidence", "execution_failed") else "escalated"}

    # Graph construction
    workflow = StateGraph(Phase2GraphState)

    workflow.add_node("formulate_fix", formulate_fix_node)
    workflow.add_node("check_confidence", check_confidence_node)
    workflow.add_node("propose_action", propose_action_node)
    workflow.add_node("request_approval", request_approval_node)
    workflow.add_node("execute_action", execute_action_node)
    workflow.add_node("monitor_convergence", monitor_convergence_node)
    workflow.add_node("escalate", escalate_node)

    # Entry routing: if an approved proposal already exists, proceed directly to execute_action
    def route_entry(state: Phase2GraphState) -> str:
        if state.get("approval_status") == "approved" and state.get("action_proposal"):
            return "execute_action"
        return "formulate_fix"

    workflow.set_conditional_entry_point(route_entry)
    workflow.add_edge("formulate_fix", "check_confidence")

    # Edge after check_confidence
    def route_after_confidence(state: Phase2GraphState) -> str:
        if state.get("outcome") == "low_confidence":
            return "escalate"
        return "propose_action"

    workflow.add_conditional_edges("check_confidence", route_after_confidence)

    # Edge after propose_action
    def route_after_propose(state: Phase2GraphState) -> str:
        if state.get("pre_approved", False):
            return "execute_action"
        return "request_approval"

    workflow.add_conditional_edges("propose_action", route_after_propose)

    # Edge after request_approval
    def route_after_approval(state: Phase2GraphState) -> str:
        if state.get("outcome") == "action_proposed":
            return END
        status = state.get("approval_status")
        if status == "approved":
            return "execute_action"
        elif status == "steer":
            return "formulate_fix"  # Operator steering re-plan loop
        return "escalate"  # Deny wins

    workflow.add_conditional_edges("request_approval", route_after_approval)

    # Edge after execute_action
    def route_after_execution(state: Phase2GraphState) -> str:
        if state.get("outcome") == "execution_failed":
            return "escalate"
        if state.get("outcome") == "pre_exec_replan":
            return "formulate_fix"
        return "monitor_convergence"

    workflow.add_conditional_edges("execute_action", route_after_execution)

    workflow.add_edge("monitor_convergence", END)
    workflow.add_edge("escalate", END)

    return workflow.compile(checkpointer=checkpointer)
