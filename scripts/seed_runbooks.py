import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Set
import uuid
import httpx
import yaml

from shared.config.loader import load_system_config, load_sag_policies_config
from shared.config.schemas import SystemConfig, SAGPoliciesConfig
from shared.harness.embeddings import generate_embedding as harness_generate_embedding
from shared.versioning import (
    RUNBOOKS_META_KEY,
    clear_pending_drift,
    compute_runbooks_digest,
    read_meta,
    set_pending_drift,
    write_meta,
)

logger = logging.getLogger(__name__)

ALLOWED_SAG_TOOLS = {
    "restart_pod",
    "scale_deployment",
    "rollback_release",
    "update_resource_limits",
    "drain_node",
    "toggle_feature_flag",
}


def extract_frontmatter_lazy(
    file_path: Path,
    max_lines: int = 100,
    max_bytes: int = 4096,
) -> Dict[str, Any]:
    """Lazily stream the first few lines/bytes of a markdown file to extract YAML frontmatter.
    Exits immediately once the frontmatter block is closed, without reading the full file.
    """
    lines: List[str] = []
    bytes_read = 0
    in_frontmatter = False
    found_closing = False

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > max_lines or bytes_read > max_bytes:
                    break
                bytes_read += len(line.encode("utf-8"))
                stripped = line.strip()
                if i == 0:
                    if stripped == "---":
                        in_frontmatter = True
                        continue
                    else:
                        break
                if in_frontmatter:
                    if stripped == "---":
                        found_closing = True
                        break
                    lines.append(line)
    except Exception:
        return {}

    if not found_closing or not lines:
        return {}

    raw_yaml = "".join(lines)
    try:
        return yaml.safe_load(raw_yaml) or {}
    except Exception:
        return {}


def lint_rule_document(
    metadata: Dict[str, Any],
    sections: Optional[List[Dict[str, Any]]] = None,
    max_rules: Optional[int] = None,
    max_words: Optional[int] = None,
    max_chars: Optional[int] = None,
    config: Optional[SAGPoliciesConfig] = None,
) -> Optional[Dict[str, Any]]:
    """Lint runbooks with type or tag 'rule'/'rules' to prevent bloated system prompt injection.
    Boundaries can be configured via sag_policies.yaml."""
    types = [str(t).lower() for t in metadata.get("types", [])]
    tags = [str(t).lower() for t in metadata.get("tags", [])]

    is_rule_doc = any(k in types or k in tags for k in ("rule", "rules"))
    if not is_rule_doc:
        return None

    # Load boundaries from config if not explicitly provided
    if max_rules is None or max_words is None or max_chars is None:
        try:
            cfg = config or load_sag_policies_config()
            linter_cfg = cfg.runbook_linter
            if max_rules is None:
                max_rules = linter_cfg.max_rules
            if max_words is None:
                max_words = linter_cfg.max_words
            if max_chars is None:
                max_chars = linter_cfg.max_chars
        except Exception:
            max_rules = max_rules if max_rules is not None else 8
            max_words = max_words if max_words is not None else 150
            max_chars = max_chars if max_chars is not None else 800

    rules = list(metadata.get("rules", []))
    rule_text = metadata.get("rule_text", "")

    # Item 1 fix: use sections to detect or enrich rule statements if empty or present
    if sections:
        for sec in sections:
            sec_title = sec.get("section_name", "").lower()
            if any(k in sec_title for k in ("rule", "policy", "guideline")):
                content = sec.get("content", "")
                if content and not rule_text:
                    rule_text = content
                for line in content.splitlines():
                    cleaned = re.sub(r"^\s*[-*\d.]+\s*", "", line).strip()
                    if cleaned and cleaned not in rules:
                        rules.append(cleaned)

    if not rule_text and rules:
        rule_text = "\n".join(rules)

    rule_count = len(rules)
    words = rule_text.split()
    word_count = len(words)
    char_count = len(rule_text)

    # Check against limits
    reasons = []
    if rule_count > max_rules:
        reasons.append(f"rule count ({rule_count}) exceeds recommended maximum ({max_rules})")
    if word_count > max_words:
        reasons.append(f"word count ({word_count}) exceeds recommended maximum ({max_words} words)")
    if char_count > max_chars:
        reasons.append(f"character count ({char_count}) exceeds recommended maximum ({max_chars} chars)")

    if not reasons:
        return None

    return {
        "id": metadata.get("id", "unknown"),
        "title": metadata.get("title", ""),
        "rule_count": rule_count,
        "max_rules": max_rules,
        "word_count": word_count,
        "max_words": max_words,
        "char_count": char_count,
        "max_chars": max_chars,
        "reasons": reasons,
    }


def confirm_rule_document(
    warning: Dict[str, Any],
    confirm_callback: Optional[Callable[[Dict[str, Any]], bool]] = None,
    interactive: bool = True,
    strict: Optional[bool] = None,
) -> bool:
    """Display warning for long rule documents and prompt operator to proceed or skip."""
    warning_box = (
        f"\n{'='*72}\n"
        f"⚠️  [AEGIS RULE LINTER WARNING] Document '{warning['id']}' has type/tag 'rule' and is potentially too verbose!\n"
        f"    Title: {warning['title']}\n"
        f"    • Rules count : {warning['rule_count']} (max recommended: {warning['max_rules']})\n"
        f"    • Word count  : {warning['word_count']} (max recommended: {warning['max_words']})\n"
        f"    • Char count  : {warning['char_count']} (max recommended: {warning['max_chars']})\n"
        f"    Reasons       : {'; '.join(warning['reasons'])}\n\n"
        f"    💡 Quirk Note: Documents marked with 'rule' are hot-injected directly into\n"
        f"       the Phase 2 system prompt (<operational_rules>) for EVERY remediation run.\n"
        f"       Verbose rules consume token budget and risk diluting agent instructions.\n"
        f"{'='*72}\n"
    )
    print(warning_box)

    if confirm_callback is not None:
        return confirm_callback(warning)

    if interactive and sys.stdin.isatty():
        try:
            choice = input(f"Proceed with loading '{warning['id']}' into index and Qdrant? [y/N/s(kip)]: ").strip().lower()
            return choice in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    # In non-interactive mode (CI / background automation)
    if strict is None:
        try:
            cfg = load_sag_policies_config()
            strict = cfg.runbook_linter.strict
        except Exception:
            strict = False
    if not strict:
        strict = os.getenv("AEGIS_STRICT_RULE_LINT", "0").lower() in ("1", "true", "yes")
    if strict:
        print(f"Strict rule linting enabled. Skipping document '{warning['id']}'.")
        return False
    return True


def parse_runbook_markdown(file_content: str, default_id: str = "") -> Dict[str, Any]:
    """Parse YAML frontmatter and split markdown content into header-based sections."""
    frontmatter: Dict[str, Any] = {}
    body = file_content
    body_start_line = 1

    if file_content.startswith("---"):
        parts = file_content.split("---", 2)
        if len(parts) >= 3:
            raw_yaml = parts[1].strip()
            frontmatter = yaml.safe_load(raw_yaml) or {}
            raw_body = parts[2]
            leading_ws_len = len(raw_body) - len(raw_body.lstrip())
            prefix_before_body = parts[0] + "---" + parts[1] + "---" + raw_body[:leading_ws_len]
            body_start_line = prefix_before_body.count("\n") + 1
            body = raw_body.strip()
    else:
        leading_ws_len = len(file_content) - len(file_content.lstrip())
        body_start_line = file_content[:leading_ws_len].count("\n") + 1
        body = file_content.strip()

    # Normalize frontmatter metadata fields
    raw_id = frontmatter.get("id") or default_id
    runbook_id = str(raw_id).strip()
    raw_title = frontmatter.get("title") or runbook_id.replace("-", " ").title()
    title = str(raw_title).strip()
    version = int(frontmatter.get("version", 1))

    # Normalize types (allow list of strings or single string)
    types: List[str] = []
    if "types" in frontmatter:
        raw_types = frontmatter["types"]
        if isinstance(raw_types, list):
            types = [str(t).strip().lower() for t in raw_types if str(t).strip()]
        elif isinstance(raw_types, str):
            types = [str(raw_types).strip().lower()]
    elif "type" in frontmatter:
        types = [str(frontmatter["type"]).strip().lower()]
    if not types:
        types = ["runbook"]

    # Normalize tags
    tags: List[str] = []
    raw_tags = frontmatter.get("tags") or []
    if isinstance(raw_tags, list):
        tags = [str(t).strip().lower() for t in raw_tags if str(t).strip()]
    elif isinstance(raw_tags, str):
        tags = [str(raw_tags).strip().lower()]

    # Normalize symptoms
    symptoms: List[str] = []
    if "symptoms" in frontmatter:
        raw_syms = frontmatter["symptoms"]
        if isinstance(raw_syms, list):
            symptoms = [str(s).strip() for s in raw_syms if str(s).strip()]
        elif isinstance(raw_syms, str):
            symptoms = [str(raw_syms).strip()]
    elif "symptom" in frontmatter:
        symptoms = [str(frontmatter["symptom"]).strip()]

    summary = str(frontmatter.get("summary") or "").strip()

    sections: List[Dict[str, Any]] = []
    rules: List[str] = []
    rule_text = ""

    # Split by markdown ## headers
    header_pattern = re.compile(r"^##\s+(.+)$", re.MULTILINE)
    matches = list(header_pattern.finditer(body))

    if not matches:
        stripped_body = body.strip()
        l_end = body_start_line + stripped_body.count("\n") if stripped_body else body_start_line
        sections.append({
            "section_name": "General",
            "content": body,
            "allowed_actions": [],
            "line_start": body_start_line,
            "line_end": l_end,
            "line_range": f"L{body_start_line}-L{l_end}",
        })
    else:
        for i, match in enumerate(matches):
            section_title = match.group(1).strip()
            start_pos = match.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            content = body[start_pos:end_pos].strip()

            raw_section_slice = body[match.start():end_pos].rstrip()
            line_start = body_start_line + body[:match.start()].count("\n")
            line_end = line_start + raw_section_slice.count("\n")

            # Extract any SAG tools mentioned in backticks
            backticks = re.findall(r"`([a-zA-Z0-9_]+)`", content)
            matched_tools = [t for t in backticks if t in ALLOWED_SAG_TOOLS]

            sections.append({
                "section_name": section_title,
                "content": content,
                "allowed_actions": list(dict.fromkeys(matched_tools)),
                "line_start": line_start,
                "line_end": line_end,
                "line_range": f"L{line_start}-L{line_end}",
            })

            # Check for operational rules or policies
            if any(k in section_title.lower() for k in ("rule", "policy", "guideline")):
                rule_text = content
                for line in content.splitlines():
                    cleaned = re.sub(r"^\s*[-*\d.]+\s*", "", line).strip()
                    if cleaned:
                        rules.append(cleaned)

    total_chunks = len(sections)
    for chunk_idx, sec in enumerate(sections, start=1):
        sec["chunk_index"] = chunk_idx
        sec["total_chunks"] = total_chunks
        sec["chunk_id"] = f"chunk {chunk_idx}/{total_chunks}"

    # Fallback summary from Symptom section or first section if missing
    if not summary:
        for sec in sections:
            if "symptom" in sec["section_name"].lower() or sec["section_name"] == "General":
                summary = sec["content"].split("\n")[0].strip()[:200]
                break

    metadata = {
        "id": runbook_id,
        "title": title,
        "version": version,
        "types": types,
        "tags": tags,
        "symptoms": symptoms,
        "summary": summary,
    }
    if rules or "rule" in types:
        metadata["rules"] = rules
        metadata["rule_text"] = rule_text

    return {
        "metadata": metadata,
        "sections": sections,
    }


def build_chunks(runbooks_dir: Path, accepted_ids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """Scan directory for markdown runbooks and build chunk payloads. Optionally filter by accepted_ids."""
    chunks: List[Dict[str, Any]] = []

    for md_file in sorted(runbooks_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        parsed = parse_runbook_markdown(text, default_id=md_file.stem)
        metadata = parsed["metadata"]
        runbook_id = metadata["id"] or md_file.stem

        if accepted_ids is not None and runbook_id not in accepted_ids:
            continue

        title = metadata["title"]
        version = metadata["version"]
        types = metadata["types"]
        symptoms = metadata["symptoms"]
        tags = metadata["tags"]
        summary = metadata["summary"]

        for sec in parsed["sections"]:
            sec_name = sec["section_name"]
            sec_content = sec["content"]
            allowed_actions = sec["allowed_actions"]

            # Deterministic UUID based on runbook ID and section name
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{runbook_id}:{sec_name}"))
            embed_text = (
                f"Document: {title} ({runbook_id}). Types: {', '.join(types)}. "
                f"Symptoms: {', '.join(symptoms)}. Section: {sec_name}. Content: {sec_content}"
            )
            content_hash = hashlib.sha256(embed_text.encode("utf-8")).hexdigest()

            chunks.append({
                "id": chunk_id,
                "runbook_id": runbook_id,
                "title": title,
                "version": version,
                "types": types,
                "section": sec_name,
                "line_start": sec.get("line_start", 1),
                "line_end": sec.get("line_end", 1),
                "line_range": sec.get("line_range", ""),
                "chunk_index": sec.get("chunk_index", 1),
                "total_chunks": sec.get("total_chunks", 1),
                "chunk_label": sec.get("chunk_id", ""),
                "symptoms": symptoms,
                "tags": tags,
                "summary": summary,
                "content": sec_content,
                "allowed_actions": allowed_actions,
                "embed_text": embed_text,
                "content_hash": content_hash,
            })

    return chunks


def generate_runbooks_index(
    runbooks_dir: Path,
    output_file: Optional[Path] = None,
    interactive: bool = True,
    confirm_callback: Optional[Callable[[Dict[str, Any]], bool]] = None,
    accepted_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Compile a lightweight Table of Contents index from runbook frontmatter and persist as JSON.
    Runs rule linter on rule documents and allows operators to skip oversized ones."""
    index_entries: List[Dict[str, Any]] = []

    for md_file in sorted(runbooks_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        parsed = parse_runbook_markdown(text, default_id=md_file.stem)
        meta = parsed["metadata"]
        runbook_id = meta["id"] or md_file.stem

        # Run rule linter
        warning = lint_rule_document(meta, parsed["sections"])
        if warning:
            proceed = confirm_rule_document(warning, confirm_callback=confirm_callback, interactive=interactive)
            if not proceed:
                print(f"Skipping oversized rule document '{runbook_id}'.")
                continue

        if accepted_ids is not None:
            accepted_ids.add(runbook_id)

        all_actions: Set[str] = set()
        section_entries: List[Dict[str, Any]] = []
        for sec in parsed["sections"]:
            all_actions.update(sec.get("allowed_actions", []))
            raw_content = " ".join(str(sec.get("content", "")).split())
            section_entries.append({
                "section_name": sec.get("section_name", "General"),
                "chunk_index": sec.get("chunk_index", 1),
                "total_chunks": sec.get("total_chunks", 1),
                "chunk_id": sec.get("chunk_id", ""),
                "line_start": sec.get("line_start", 1),
                "line_end": sec.get("line_end", 1),
                "line_range": sec.get("line_range", ""),
                "allowed_actions": sec.get("allowed_actions", []),
                "excerpt": raw_content[:280],
            })

        entry: Dict[str, Any] = {
            "id": runbook_id,
            "title": meta["title"],
            "types": meta["types"],
            "tags": meta["tags"],
            "symptoms": meta["symptoms"],
            "summary": meta["summary"],
            "allowed_actions": sorted(list(all_actions)),
            "sections": section_entries,
        }
        if "rules" in meta:
            entry["rules"] = meta["rules"]
        if "rule_text" in meta:
            entry["rule_text"] = meta["rule_text"]

        index_entries.append(entry)

    if output_file is not None:
        try:
            digest = compute_runbooks_digest(index_entries)
            meta_file = output_file.parent / ".runbooks_index_meta.json"
            existing_sha = ""
            if meta_file.is_file():
                try:
                    existing_sha = str(json.loads(meta_file.read_text(encoding="utf-8")).get("sha256") or "")
                except Exception:
                    existing_sha = ""

            if not (output_file.is_file() and existing_sha == digest):
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps(index_entries, indent=2), encoding="utf-8")
                meta_file.write_text(
                    json.dumps(
                        {
                            "sha256": digest,
                            "entry_count": len(index_entries),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except OSError as exc:
            logger.warning("Could not persist runbooks index to %s: %s", output_file, exc)

    return index_entries


async def generate_embedding(
    text: str,
    ollama_url: Optional[str] = None,
    model: Optional[str] = None,
    client: Optional[Any] = None,
    provider: Optional[str] = None,
) -> List[float]:
    """Request vector embedding using the shared multi-provider harness."""
    sys_cfg = load_system_config()
    clean_provider = provider or sys_cfg.qdrant.embedding_provider
    clean_model = model or sys_cfg.qdrant.embedding_model
    clean_url = ollama_url or sys_cfg.ollama.url
    return await harness_generate_embedding(
        text=text,
        provider=clean_provider,
        model=clean_model,
        client=client,
        base_url=clean_url if clean_provider == "ollama" else None,
    )


async def seed_qdrant_runbooks(
    config: Optional[SystemConfig] = None,
    runbooks_dir: Optional[Path] = None,
    client: Optional[httpx.AsyncClient] = None,
    index_output_path: Optional[Path] = None,
    interactive: bool = True,
    confirm_callback: Optional[Callable[[Dict[str, Any]], bool]] = None,
    force: bool = False,
    collection: Optional[str] = None,
) -> int:
    """Read runbooks, compile TOC index, generate embeddings, and upsert points to Qdrant.
    Uses persistent delta hashing cache (.seed_cache.json) to skip unchanged runbooks on reboot."""
    sys_config = config or load_system_config()
    qdrant_url = (os.getenv("QDRANT_URL") or sys_config.qdrant.url).rstrip("/")
    collection = collection or os.getenv("QDRANT_COLLECTION") or sys_config.qdrant.collection
    ollama_url = (os.getenv("OLLAMA_URL") or sys_config.ollama.url).rstrip("/")
    provider = os.getenv("EMBEDDING_PROVIDER") or sys_config.qdrant.embedding_provider
    model = os.getenv("EMBEDDING_MODEL") or sys_config.qdrant.embedding_model

    target_dir = runbooks_dir or (Path(__file__).resolve().parent.parent / "seed" / "runbooks")

    # 1. Generate local runbooks index (TOC) with rule linter
    accepted_ids: Set[str] = set()
    index_file = index_output_path or (target_dir.parent / "runbooks_index.json")
    generate_runbooks_index(
        target_dir,
        output_file=index_file,
        interactive=interactive,
        confirm_callback=confirm_callback,
        accepted_ids=accepted_ids,
    )

    chunks = build_chunks(target_dir, accepted_ids=accepted_ids)

    if not chunks:
        return 0

    # 2. Check delta cache to skip unchanged chunks
    cache_file = target_dir.parent / ".seed_cache.json"
    existing_cache: Dict[str, str] = {}
    if cache_file.exists() and not force:
        try:
            existing_cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            existing_cache = {}

    chunks_to_embed = [c for c in chunks if existing_cache.get(c["id"]) != c.get("content_hash")]

    own_client = False
    if client is None:
        client = httpx.AsyncClient()
        own_client = True

    try:
        # 3. Check if collection exists in Qdrant
        check_resp = await client.get(f"{qdrant_url}/collections/{collection}")
        need_create = check_resp.status_code == 404

        if check_resp.status_code == 200 and force:
            logger.info("Force recreating collection '%s'", collection)
            del_resp = await client.delete(f"{qdrant_url}/collections/{collection}", timeout=10.0)
            del_resp.raise_for_status()
            need_create = True
            existing_cache = {}

        if need_create:
            # Probe embedding dimension
            probe_vector = await generate_embedding("probe", ollama_url, model, provider=provider)
            target_dim = len(probe_vector)
            create_payload = {
                "vectors": {
                    "size": target_dim,
                    "distance": "Cosine",
                }
            }
            create_resp = await client.put(
                f"{qdrant_url}/collections/{collection}",
                json=create_payload,
                timeout=10.0,
            )
            create_resp.raise_for_status()

        # 4. If all chunks are up-to-date and collection exists without recreate, skip embedding entirely!
        if not force and not need_create and not chunks_to_embed:
            return len(chunks)

        # 5. Embed only new or modified chunks (or all chunks if collection was recreated or forced)
        target_chunks = chunks if (need_create or force) else chunks_to_embed
        points = []
        for chunk in target_chunks:
            vector = await generate_embedding(
                chunk["embed_text"],
                ollama_url,
                model,
                provider=provider,
            )
            payload = {
                "runbook_id": chunk["runbook_id"],
                "title": chunk["title"],
                "version": chunk["version"],
                "types": chunk["types"],
                "section": chunk["section"],
                "line_start": chunk.get("line_start"),
                "line_end": chunk.get("line_end"),
                "line_range": chunk.get("line_range", ""),
                "chunk_index": chunk.get("chunk_index"),
                "total_chunks": chunk.get("total_chunks"),
                "chunk_label": chunk.get("chunk_label", ""),
                "symptoms": chunk["symptoms"],
                "tags": chunk["tags"],
                "summary": chunk["summary"],
                "content": chunk["content"],
                "allowed_actions": chunk["allowed_actions"],
                "content_hash": chunk.get("content_hash", ""),
            }
            points.append({
                "id": chunk["id"],
                "vector": vector,
                "payload": payload,
            })
            existing_cache[chunk["id"]] = chunk.get("content_hash", "")

        # 6. Upsert points into Qdrant
        if points:
            upsert_resp = await client.put(
                f"{qdrant_url}/collections/{collection}/points?wait=true",
                json={"points": points},
                timeout=30.0,
            )
            upsert_resp.raise_for_status()

            # Save updated cache to disk
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(existing_cache, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not persist seed cache to %s: %s", cache_file, exc)

        return len(chunks)

    finally:
        if own_client:
            await client.aclose()


def _resolve_runbooks_paths(
    runbooks_dir: Optional[Path] = None,
    index_output_path: Optional[Path] = None,
) -> tuple[Path, Path]:
    target_dir = runbooks_dir or (Path(__file__).resolve().parent.parent / "seed" / "runbooks")
    index_file = index_output_path or (target_dir.parent / "runbooks_index.json")
    return target_dir, index_file


def _read_disk_runbook_meta(index_file: Path) -> Dict[str, Any]:
    meta_file = index_file.parent / ".runbooks_index_meta.json"
    if meta_file.is_file():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    if index_file.is_file():
        try:
            entries = json.loads(index_file.read_text(encoding="utf-8"))
            if isinstance(entries, list):
                return {"sha256": compute_runbooks_digest(entries), "entry_count": len(entries), "version": 1}
        except Exception:
            pass
    return {}


async def sync_runbook_index_and_qdrant(
    config: Optional[SystemConfig] = None,
    redis_client: Optional[Any] = None,
    *,
    force: bool = False,
    updated_by: str = "system",
    runbooks_dir: Optional[Path] = None,
    index_output_path: Optional[Path] = None,
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile runbook index digest, skip unchanged syncs unless forced, and update Qdrant + Redis version."""
    sys_cfg = config or load_system_config()
    target_dir, index_file = _resolve_runbooks_paths(runbooks_dir, index_output_path)
    live_entries = generate_runbooks_index(target_dir, output_file=None, interactive=False)
    live_sha = compute_runbooks_digest(live_entries)

    redis_meta = read_meta(redis_client, RUNBOOKS_META_KEY)
    disk_meta = _read_disk_runbook_meta(index_file)
    old_meta = redis_meta or disk_meta
    stored_sha = str(old_meta.get("sha256") or "")
    old_version = int(old_meta.get("version") or 0)

    if not force and stored_sha and stored_sha == live_sha and index_file.is_file():
        clear_pending_drift(redis_client, "runbooks")
        if not redis_meta:
            write_meta(
                redis_client,
                RUNBOOKS_META_KEY,
                {**old_meta, "sha256": live_sha, "version": max(1, old_version)},
            )
        return {
            "status": "unchanged",
            "changed": False,
            "old_sha": stored_sha,
            "sha256": live_sha,
            "version": max(1, old_version),
            "entry_count": len(live_entries),
        }

    # Execute Qdrant seed (non-interactive) and persist index JSON
    try:
        seeded_chunks = await seed_qdrant_runbooks(
            config=sys_cfg,
            runbooks_dir=target_dir,
            index_output_path=index_file,
            interactive=False,
            force=force,
            collection=collection,
        )
    except Exception as exc:
        logger.warning("Qdrant sync partial/unavailable during runbook index sync (%s); updating index locally", exc)
        seeded_chunks = 0
    generate_runbooks_index(target_dir, output_file=index_file, interactive=False)

    new_version = old_version + 1
    now_iso = datetime.now(timezone.utc).isoformat()
    meta_payload = {
        "version": new_version,
        "sha256": live_sha,
        "updated_at": now_iso,
        "updated_by": updated_by,
        "entry_count": len(live_entries),
        "seeded_chunks": seeded_chunks,
    }
    write_meta(redis_client, RUNBOOKS_META_KEY, meta_payload)
    try:
        meta_file = index_file.parent / ".runbooks_index_meta.json"
        meta_file.write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed writing companion runbooks meta file: %s", exc)

    clear_pending_drift(redis_client, "runbooks")
    return {
        "status": "rebuilt" if force else "synced",
        "changed": True,
        "old_sha": stored_sha,
        "sha256": live_sha,
        "version": new_version,
        "entry_count": len(live_entries),
        "seeded_chunks": seeded_chunks,
    }


def detect_runbook_drift(
    runbooks_dir: Optional[Path] = None,
    redis_client: Optional[Any] = None,
    index_output_path: Optional[Path] = None,
    config: Optional[SystemConfig] = None,
    notify: bool = True,
) -> Optional[Dict[str, Any]]:
    """Detect SHA-256 drift between runbook markdown sources and active runbooks index without overwriting."""
    target_dir, index_file = _resolve_runbooks_paths(runbooks_dir, index_output_path)
    if not target_dir.is_dir():
        return None

    live_entries = generate_runbooks_index(target_dir, output_file=None, interactive=False)
    live_sha = compute_runbooks_digest(live_entries)

    old_meta = read_meta(redis_client, RUNBOOKS_META_KEY) or _read_disk_runbook_meta(index_file)
    stored_sha = str(old_meta.get("sha256") or "")

    if live_sha == stored_sha:
        clear_pending_drift(redis_client, "runbooks")
        return None

    stored_ids: Set[str] = set()
    if index_file.is_file():
        try:
            prev = json.loads(index_file.read_text(encoding="utf-8"))
            stored_ids = {str(x.get("id", "")) for x in prev if isinstance(x, dict) and x.get("id")}
        except Exception:
            stored_ids = set()
    live_ids = {str(x.get("id", "")) for x in live_entries if x.get("id")}

    drift_record = {
        "target": "runbooks",
        "status": "pending",
        "old_sha": stored_sha,
        "new_sha": live_sha,
        "current_version": int(old_meta.get("version") or 0),
        "summary": {
            "added_runbooks": sorted(live_ids - stored_ids),
            "removed_runbooks": sorted(stored_ids - live_ids),
            "live_entry_count": len(live_entries),
        },
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    set_pending_drift(redis_client, "runbooks", drift_record)

    from shared.audit.tracker import record_interaction
    record_interaction(
        redis_client,
        direction="internal",
        category="drift_detected",
        actor="runbooks_sync",
        entity_type="internal_service",
        target="runbooks",
        action="detect_drift",
        external=False,
        status="pending_approval",
        details={"old_sha": stored_sha[:12], "new_sha": live_sha[:12], "summary": drift_record["summary"]},
    )

    if notify and config is not None:
        try:
            from services.api_server.notifications import dispatch_notification_background
            dispatch_notification_background(
                event="drift_detected",
                incident_id="system-drift-runbooks",
                data=drift_record,
                config=config,
            )
        except Exception as exc:
            logger.debug("Failed dispatching runbooks drift notification: %s", exc)

    return drift_record


if __name__ == "__main__":
    force_opt = "--force" in sys.argv
    coll_opt = None
    for idx, arg in enumerate(sys.argv):
        if arg == "--collection" and idx + 1 < len(sys.argv):
            coll_opt = sys.argv[idx + 1]

    # Run for the specified collection or both aegis_runbooks and aegis_runbooks_boutique if not specified
    target_colls = [coll_opt] if coll_opt else ["aegis_runbooks", "aegis_runbooks_boutique"]
    for coll in target_colls:
        count = asyncio.run(seed_qdrant_runbooks(force=force_opt, collection=coll))
        print(f"Successfully seeded {count} runbook section chunks into Qdrant collection '{coll}' and updated seed/runbooks_index.json.")
