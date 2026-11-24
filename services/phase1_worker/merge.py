"""Merge logic for Phase 1 investigator agent findings.

Per Architecture §4.4 and Decision D1:
- Programmatic aggregation, NOT an LLM merge.
- Evidence raw_output values are preserved strictly verbatim.
- Corroboration scoring ranks shared signals/services higher.
"""

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Set

from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding, EvidenceItem, MergedFinding


DEFAULT_REMEDIATION_MAX_PER_AGENT = 3
DEFAULT_REMEDIATION_MAX_TOTAL = 5


def _extract_tokens(text: str) -> Set[str]:
    """Extract lowercase alphanumeric tokens (min length 3) for entity matching."""
    if not text:
        return set()
    tokens = re.findall(r"[a-zA-Z0-9_\-\.]{3,}", text.lower())
    # Filter common noisy tokens
    stop_words = {
        "the", "and", "for", "with", "from", "that", "this", "true", "false",
        "null", "none", "error", "info", "warn", "warning", "status", "http"
    }
    return {t for t in tokens if t not in stop_words}


def score_evidence_corroboration(
    findings: List[AgentFinding]
) -> List[EvidenceItem]:
    """Rank all unique evidence items across findings with stratified top-per-agent priority.

    Preserves every unique evidence item (nothing is dropped from MergedFinding) while
    ordering each agent's strongest item first so downstream consumers taking a 5-item
    budget see all active agents regardless of confidence score.
    """
    if not findings:
        return []

    # Map each agent to its set of mentioned tokens across hypothesis and verbatim signals
    agent_tokens: Dict[AgentRole, Set[str]] = {}
    for f in findings:
        tokens = _extract_tokens(f.hypothesis)
        for val in f.verbatim_signals.values():
            tokens.update(_extract_tokens(str(val)))
        for ev in f.evidence:
            tokens.update(_extract_tokens(ev.query_or_cmd))
        agent_tokens[f.agent_role] = tokens

    scored_items: List[tuple[int, int, AgentRole, EvidenceItem]] = []
    seen_ev_keys: Set[tuple[str, str]] = set()
    idx = 0

    for f in findings:
        for ev in f.evidence:
            ev_key = (ev.source_tool, ev.query_or_cmd)
            if ev_key in seen_ev_keys:
                continue
            seen_ev_keys.add(ev_key)
            ev_tokens = _extract_tokens(ev.query_or_cmd) | _extract_tokens(ev.raw_output)
            corroborating_agents = 0
            for other_role, other_tokens in agent_tokens.items():
                if other_role != f.agent_role and (ev_tokens & other_tokens):
                    corroborating_agents += 1

            scored_items.append((corroborating_agents, idx, f.agent_role, ev))
            idx += 1

    # Prioritize top item per agent first so stratified slicing never starves any role,
    # while keeping ALL remaining items appended in corroboration order (0 dropped).
    by_score = sorted(scored_items, key=lambda item: (item[0], -item[1]), reverse=True)
    first_per_role: List[tuple[int, int, AgentRole, EvidenceItem]] = []
    remainder: List[tuple[int, int, AgentRole, EvidenceItem]] = []
    covered_roles: Set[AgentRole] = set()

    for entry in by_score:
        role = entry[2]
        if role not in covered_roles:
            covered_roles.add(role)
            first_per_role.append(entry)
        else:
            remainder.append(entry)

    first_per_role.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    remainder.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [item[3] for item in first_per_role + remainder]


def merge_findings(
    findings: List[AgentFinding],
    incident_id: str = "unknown"
) -> MergedFinding:
    """Merge findings from multiple investigator agents into a MergedFinding.
    
    Args:
        findings: List of AgentFinding objects from Phase 1 investigators.
        incident_id: Identifier of the incident being investigated.
        
    Returns:
        MergedFinding containing ranked evidence, verbatim signals, and merged confidence.
    """
    if not findings:
        return MergedFinding(
            incident_id=incident_id,
            primary_hypothesis="No investigator findings provided",
            merged_confidence=0.0,
            findings_by_agent={},
            ranked_findings=[],
            all_evidence=[],
            verbatim_signals={},
            infra_flags={},
            corroboration_score=0.0,
            merged_at=datetime.now(timezone.utc),
        )

    def _grounding_rank(f: AgentFinding) -> int:
        hyp = (f.hypothesis or "").lower()
        status = str((f.verbatim_signals or {}).get("tool_execution_status", "")).lower()
        if hyp.startswith("[ungrounded") or "no tools called" in hyp or "no tools called" in status:
            return 0
        if f.evidence:
            return 2
        return 1

    # 1. Rank ALL investigator findings without dropping any agent or evidence item;
    #    Phase 2 performs stratified prompt budgeting (max 3 per agent, max 5 total).
    priority_order = {
        AgentRole.INFRASTRUCTURE_INVESTIGATOR: 3,
        AgentRole.LOG_TRACE_INVESTIGATOR: 2,
        AgentRole.CHANGE_CONFIG_INVESTIGATOR: 1,
    }
    sorted_by_confidence = sorted(
        findings,
        key=lambda f: (
            _grounding_rank(f),
            f.confidence,
            priority_order.get(f.agent_role, 0),
        ),
        reverse=True,
    )
    findings_by_agent: Dict[AgentRole, AgentFinding] = {}
    for f in sorted_by_confidence:
        if f.agent_role not in findings_by_agent:
            findings_by_agent[f.agent_role] = f

    primary_hypothesis = sorted_by_confidence[0].hypothesis

    # 3. Combine verbatim signals (verbatim, never paraphrased)
    combined_signals: Dict[str, Any] = {}
    for f in sorted_by_confidence:
        combined_signals.update(f.verbatim_signals)

    # 4. Merge infra_flags (A1 is authoritative, but collect from any reporting agent)
    combined_infra_flags: Dict[str, Any] = {}
    if AgentRole.INFRASTRUCTURE_INVESTIGATOR in findings_by_agent:
        combined_infra_flags.update(
            findings_by_agent[AgentRole.INFRASTRUCTURE_INVESTIGATOR].infra_flags
        )
    for f in sorted_by_confidence:
        if f.agent_role != AgentRole.INFRASTRUCTURE_INVESTIGATOR:
            for k, v in f.infra_flags.items():
                if k not in combined_infra_flags:
                    combined_infra_flags[k] = v

    # 5. Pool and rank all evidence items cleanly without dropping
    ranked_evidence = score_evidence_corroboration(sorted_by_confidence)

    # 6. Calculate corroboration score across agents
    # Measure overlap in hypotheses/tokens across agents
    total_pairs = 0
    agreed_pairs = 0
    roles = list(findings_by_agent.keys())
    for i in range(len(roles)):
        for j in range(i + 1, len(roles)):
            total_pairs += 1
            tokens_i = _extract_tokens(findings_by_agent[roles[i]].hypothesis)
            tokens_j = _extract_tokens(findings_by_agent[roles[j]].hypothesis)
            if tokens_i & tokens_j:
                agreed_pairs += 1

    corroboration_score = (agreed_pairs / total_pairs) if total_pairs > 0 else 0.0
    corroboration_score = round(min(max(corroboration_score, 0.0), 1.0), 2)

    # 7. Calculate merged confidence
    avg_confidence = sum(f.confidence for f in findings) / len(findings)
    # Blend average confidence with corroboration bonus/penalty
    merged_confidence = avg_confidence * 0.8 + corroboration_score * 0.2
    merged_confidence = round(min(max(merged_confidence, 0.0), 1.0), 2)

    return MergedFinding(
        incident_id=incident_id,
        primary_hypothesis=primary_hypothesis,
        merged_confidence=merged_confidence,
        findings_by_agent=findings_by_agent,
        ranked_findings=sorted_by_confidence,
        all_evidence=ranked_evidence,
        verbatim_signals=combined_signals,
        infra_flags=combined_infra_flags,
        corroboration_score=corroboration_score,
        merged_at=datetime.now(timezone.utc),
    )
