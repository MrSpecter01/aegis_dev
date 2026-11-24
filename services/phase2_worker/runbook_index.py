import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def load_runbooks_index(
    index_path: Optional[Path] = None,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load runbooks Table of Contents index from disk."""
    candidate_paths = []
    if index_path:
        candidate_paths.append(index_path)
    env_path = os.getenv("AEGIS_RUNBOOKS_INDEX_PATH")
    if env_path:
        candidate_paths.append(Path(env_path))

    if namespace and namespace.strip():
        clean_ns = namespace.strip()
        candidate_paths.extend([
            Path(f"testbeds/{clean_ns}/runbooks_index.json"),
            Path(__file__).resolve().parent.parent.parent / "testbeds" / clean_ns / "runbooks_index.json",
        ])

    testbed = os.getenv("AEGIS_TESTBED")
    if testbed:
        candidate_paths.extend([
            Path(f"testbeds/{testbed}/runbooks_index.json"),
            Path(__file__).resolve().parent.parent.parent / "testbeds" / testbed / "runbooks_index.json",
        ])

    # Default workspace locations
    candidate_paths.extend([
        Path("seed/runbooks_index.json"),
        Path(__file__).resolve().parent.parent.parent / "seed" / "runbooks_index.json",
    ])

    for path in candidate_paths:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception as e:
                logger.warning("Failed to load runbooks index from %s: %s", path, e)

    return []


def extract_operational_rules(index: List[Dict[str, Any]]) -> str:
    """Extract operational rules from all documents flagged with 'rule' in types."""
    rule_blocks: List[str] = []
    for entry in index:
        types = [t.lower() for t in entry.get("types", [])]
        if "rule" in types:
            doc_id = entry.get("id", "unknown")
            title = entry.get("title", doc_id)
            rules = entry.get("rules", [])
            rule_text = entry.get("rule_text", "")
            loc_suffix = ""
            for sec in entry.get("sections", []):
                if isinstance(sec, dict) and any(
                    k in str(sec.get("section_name", "")).lower() for k in ("rule", "policy", "guideline")
                ):
                    l_range = sec.get("line_range", "")
                    c_id = sec.get("chunk_id", "")
                    loc_parts = [p for p in (l_range, c_id) if p]
                    if loc_parts:
                        loc_suffix = f" [{', '.join(loc_parts)}]"
                    break

            block_lines = [f"### Rules from {title} ({doc_id}){loc_suffix}:"]
            if rules:
                for r in rules:
                    block_lines.append(f"- {r}")
            elif rule_text:
                block_lines.append(rule_text.strip())
            else:
                block_lines.append(f"- {entry.get('summary', '')}")

            rule_blocks.append("\n".join(block_lines))

    return "\n\n".join(rule_blocks).strip()


def format_catalog_prompt(index: List[Dict[str, Any]]) -> str:
    """Format index entries into a Table of Contents summary for prompt context."""
    if not index:
        return "No operational runbooks indexed."

    lines: List[str] = [
        "Available Operational Runbooks & Historical Postmortems (Table of Contents):"
    ]
    for item in index:
        doc_id = item.get("id", "")
        title = item.get("title", doc_id)
        types = ", ".join(item.get("types", []))
        symptoms = ", ".join(item.get("symptoms", []))
        summary = item.get("summary", "")
        tools = ", ".join(item.get("allowed_actions", []))

        lines.append(f"- ID: `{doc_id}` | Title: {title} | Types: [{types}]")
        if symptoms:
            lines.append(f"  Symptoms: {symptoms}")
        if summary:
            lines.append(f"  Summary: {summary}")
        if tools:
            lines.append(f"  Allowed Tools: {tools}")
        sections = item.get("sections") or []
        if isinstance(sections, list) and sections:
            sec_labels: List[str] = []
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                s_name = sec.get("section_name", "General")
                s_lines = sec.get("line_range", "")
                s_chunk = sec.get("chunk_id", "")
                meta = ", ".join(p for p in (s_lines, s_chunk) if p)
                sec_labels.append(f"{s_name} ({meta})" if meta else s_name)
            if sec_labels:
                lines.append(f"  Sections: {'; '.join(sec_labels)}")

    return "\n".join(lines)


def resolve_runbook_section_metadata(
    runbook_id: str = "",
    section: str = "",
    namespace: Optional[str] = None,
    index: Optional[List[Dict[str, Any]]] = None,
    tool_name: str = "",
) -> Optional[Dict[str, Any]]:
    """Resolve authoritative runbook citation metadata (book_name, section, line_range, chunk_id, referenced_text)."""
    target_id = (runbook_id or "").strip()
    target_section = (section or "").strip()
    target_tool = (tool_name or "").strip()
    loaded_index = index if index is not None else load_runbooks_index(namespace=namespace)

    def _pick_section(sections: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not sections:
            return None
        if target_section:
            for s in sections:
                if str(s.get("section_name", "")).strip().lower() == target_section.lower():
                    return s
            for s in sections:
                if target_section.lower() in str(s.get("section_name", "")).strip().lower():
                    return s
        if target_tool:
            for s in sections:
                if target_tool in (s.get("allowed_actions") or []):
                    return s
        for pref in ("remediation steps", "immediate mitigation", "remediation"):
            for s in sections:
                if pref in str(s.get("section_name", "")).strip().lower():
                    return s
        return sections[0]

    # 1. Check in-memory or disk index first
    matched_entry: Optional[Dict[str, Any]] = None
    for item in loaded_index:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        item_title = str(item.get("title", "")).strip()
        if target_id and (item_id.lower() == target_id.lower() or item_title.lower() == target_id.lower()):
            matched_entry = item
            break
    if matched_entry is None and target_tool:
        for item in loaded_index:
            if isinstance(item, dict) and target_tool in (item.get("allowed_actions") or []):
                matched_entry = item
                break

    if matched_entry is not None:
        doc_id = str(matched_entry.get("id", target_id)).strip()
        book_name = str(matched_entry.get("title") or doc_id).strip()
        secs = matched_entry.get("sections") or []
        if isinstance(secs, list) and secs:
            chosen = _pick_section([s for s in secs if isinstance(s, dict)])
            if chosen:
                return {
                    "runbook_id": doc_id,
                    "book_name": book_name,
                    "section": str(chosen.get("section_name", "General")),
                    "line_range": str(chosen.get("line_range", "")),
                    "chunk_id": str(chosen.get("chunk_id") or chosen.get("chunk_label") or ""),
                    "referenced_text": str(chosen.get("excerpt") or chosen.get("content") or matched_entry.get("summary") or "")[:300],
                }
        target_id = doc_id

    # 2. Fallback: parse the runbook markdown file directly from disk to get exact lines & chunks
    candidate_files: List[Path] = []
    if target_id:
        roots: List[Path] = []
        if namespace and namespace.strip():
            ns = namespace.strip()
            roots.extend([
                Path(f"testbeds/{ns}/runbooks"),
                Path(__file__).resolve().parent.parent.parent / "testbeds" / ns / "runbooks",
            ])
        testbed = os.getenv("AEGIS_TESTBED")
        if testbed:
            roots.extend([
                Path(f"testbeds/{testbed}/runbooks"),
                Path(__file__).resolve().parent.parent.parent / "testbeds" / testbed / "runbooks",
            ])
        roots.extend([
            Path("seed/runbooks"),
            Path(__file__).resolve().parent.parent.parent / "seed" / "runbooks",
        ])
        for root in roots:
            candidate_files.append(root / f"{target_id}.md")

    for md_path in candidate_files:
        if md_path.is_file():
            try:
                from scripts.seed_runbooks import parse_runbook_markdown
                parsed = parse_runbook_markdown(md_path.read_text(encoding="utf-8"), default_id=md_path.stem)
                meta = parsed.get("metadata", {})
                secs = parsed.get("sections", [])
                chosen = _pick_section(secs)
                if chosen:
                    raw_excerpt = " ".join(str(chosen.get("content", "")).split())[:300]
                    return {
                        "runbook_id": str(meta.get("id") or md_path.stem),
                        "book_name": str(meta.get("title") or meta.get("id") or md_path.stem),
                        "section": str(chosen.get("section_name", "General")),
                        "line_range": str(chosen.get("line_range", "")),
                        "chunk_id": str(chosen.get("chunk_id", "")),
                        "referenced_text": raw_excerpt,
                    }
            except Exception as exc:
                logger.warning("Failed resolving runbook metadata from %s: %s", md_path, exc)

    if matched_entry is not None:
        return {
            "runbook_id": str(matched_entry.get("id", "")),
            "book_name": str(matched_entry.get("title") or matched_entry.get("id", "")),
            "section": target_section or "General",
            "line_range": "",
            "chunk_id": "",
            "referenced_text": str(matched_entry.get("summary", ""))[:300],
        }
    return None


def should_use_two_step_catalog(
    index: List[Dict[str, Any]],
    mode: str = "auto",
    max_inline: int = 15,
) -> bool:
    """Decide whether Phase 2 uses 2-step search_runbook_catalog -> qdrant_search instead of inline catalog."""
    clean_mode = (mode or "auto").strip().lower()
    if clean_mode == "direct":
        return False
    if clean_mode == "two_step":
        return True
    catalog_count = sum(1 for item in (index or []) if isinstance(item, dict))
    threshold = max(1, int(max_inline or 15))
    return catalog_count > threshold


def search_runbook_catalog(
    index: List[Dict[str, Any]],
    query: str = "",
    symptoms: str = "",
    service: str = "",
    limit: int = 5,
) -> Dict[str, Any]:
    """Search the compiled runbooks TOC index by query, symptoms, and service name (Step 1 of 2-step RAG)."""
    entries = [e for e in (index or []) if isinstance(e, dict)]
    safe_limit = max(1, min(int(limit or 5), 20))
    raw_terms = " ".join(part for part in (query, symptoms, service) if part).lower()
    tokens = [tok for tok in re.findall(r"[a-z0-9_-]+", raw_terms) if len(tok) >= 2]

    scored: List[tuple[float, Dict[str, Any]]] = []
    for item in entries:
        doc_id = str(item.get("id", ""))
        title = str(item.get("title", doc_id))
        sym_text = " ".join(str(s) for s in (item.get("symptoms") or []))
        tags_text = " ".join(str(t) for t in (item.get("tags") or []))
        summary = str(item.get("summary", ""))
        tools_text = " ".join(str(a) for a in (item.get("allowed_actions") or []))
        sections = item.get("sections") or []
        sec_text = " ".join(
            f"{s.get('section_name', '')} {s.get('excerpt', '')}"
            for s in sections
            if isinstance(s, dict)
        )

        if not tokens:
            score = 1.0
        else:
            score = 0.0
            for tok in tokens:
                if tok in doc_id.lower():
                    score += 3.0
                if tok in title.lower():
                    score += 3.0
                if tok in sym_text.lower():
                    score += 2.5
                if tok in tags_text.lower() or tok in tools_text.lower():
                    score += 2.0
                if tok in summary.lower():
                    score += 1.5
                if tok in sec_text.lower():
                    score += 1.0
            if service and service.strip().lower() in f"{doc_id} {title} {sym_text} {tags_text} {summary}".lower():
                score += 2.5

        if score > 0 or not tokens:
            compact_sections = [
                {
                    "section_name": s.get("section_name", "General"),
                    "line_range": s.get("line_range", ""),
                    "chunk_id": s.get("chunk_id", ""),
                }
                for s in sections
                if isinstance(s, dict)
            ]
            scored.append((
                score,
                {
                    "id": doc_id,
                    "title": title,
                    "types": item.get("types", []),
                    "symptoms": item.get("symptoms", []),
                    "summary": summary,
                    "allowed_actions": item.get("allowed_actions", []),
                    "sections": compact_sections,
                    "score": round(score, 2),
                },
            ))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    matches = [item for _, item in scored[:safe_limit]]
    return {
        "mode": "two_step_catalog",
        "total_indexed": len(entries),
        "match_count": len(matches),
        "matches": matches,
    }
