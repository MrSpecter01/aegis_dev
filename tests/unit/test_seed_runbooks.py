from pathlib import Path
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.seed_runbooks import (
    parse_runbook_markdown,
    build_chunks,
    generate_embedding,
    seed_qdrant_runbooks,
    generate_runbooks_index,
    lint_rule_document,
    confirm_rule_document,
    ALLOWED_SAG_TOOLS,
)
from shared.config.schemas import SystemConfig


def test_parse_runbook_markdown_structured():
    sample_md = """---
id: sample-runbook
title: Sample Pod Remediation
version: 2
types: [postmortem, rule]
symptoms: [Pods restarting constantly, Memory leak]
tags: [k8s, restart]
summary: Sample summary for pod remediation
---

## Symptom
Multiple pods are restarting.

## Operational Rules
1. Never drain nodes during active restart loop.
2. Verify rollback feasibility first.

## Remediation Steps
1. Restart pod: `restart_pod`
2. If invalid: `rollback_release`
3. Unknown tool: `unknown_tool_xyz`
"""
    parsed = parse_runbook_markdown(sample_md)
    assert parsed["metadata"]["id"] == "sample-runbook"
    assert parsed["metadata"]["title"] == "Sample Pod Remediation"
    assert parsed["metadata"]["version"] == 2
    assert parsed["metadata"]["types"] == ["postmortem", "rule"]
    assert parsed["metadata"]["symptoms"] == ["Pods restarting constantly", "Memory leak"]
    assert parsed["metadata"]["tags"] == ["k8s", "restart"]
    assert parsed["metadata"]["summary"] == "Sample summary for pod remediation"
    assert len(parsed["metadata"]["rules"]) == 2
    assert "Never drain nodes" in parsed["metadata"]["rules"][0]

    sections = parsed["sections"]
    assert len(sections) == 3
    assert sections[0]["section_name"] == "Symptom"
    assert "Multiple pods" in sections[0]["content"]

    assert sections[2]["section_name"] == "Remediation Steps"
    assert "restart_pod" in sections[2]["allowed_actions"]
    assert "rollback_release" in sections[2]["allowed_actions"]
    assert "unknown_tool_xyz" not in sections[2]["allowed_actions"]


def test_parse_runbook_markdown_no_frontmatter():
    sample_md = "## General\nSingle section without frontmatter"
    parsed = parse_runbook_markdown(sample_md, default_id="fallback-id")
    assert parsed["metadata"]["id"] == "fallback-id"
    assert parsed["metadata"]["types"] == ["runbook"]
    assert len(parsed["sections"]) == 1
    assert parsed["sections"][0]["section_name"] == "General"


def test_generate_runbooks_index(tmp_path):
    from scripts.seed_runbooks import generate_runbooks_index
    import json

    doc_file = tmp_path / "pm-rule.md"
    doc_file.write_text("""---
id: test-pm
title: Test Postmortem
types: [postmortem, rule]
tags: [dns]
symptoms: [503 error]
summary: Test postmortem summary
---

## Operational Rules
- Follow DNS policy carefully.

## Remediation Steps
Call `restart_pod`
""", encoding="utf-8")

    index_out = tmp_path / "runbooks_index.json"
    index = generate_runbooks_index(tmp_path, output_file=index_out)

    assert len(index) == 1
    assert index[0]["id"] == "test-pm"
    assert index[0]["types"] == ["postmortem", "rule"]
    assert index[0]["rules"] == ["Follow DNS policy carefully."]
    assert index[0]["allowed_actions"] == ["restart_pod"]

    assert index_out.is_file()
    saved = json.loads(index_out.read_text(encoding="utf-8"))
    assert saved[0]["id"] == "test-pm"


def test_build_chunks_scans_seed_directory():
    runbooks_dir = Path("testbeds/boutique/runbooks") if Path("testbeds/boutique/runbooks").exists() else Path("seed/runbooks")
    chunks = build_chunks(runbooks_dir)
    assert len(chunks) >= 6  # At least 6 runbooks, multiple sections each

    runbook_ids = {c["runbook_id"] for c in chunks}
    assert "oom-killed-pod" in runbook_ids
    assert "crash-loop-backoff" in runbook_ids
    assert "high-error-rate" in runbook_ids
    assert "db-connection-exhaustion" in runbook_ids
    assert "node-pressure-eviction" in runbook_ids
    assert "network-latency" in runbook_ids

    # Check ID uniqueness
    chunk_ids = [c["id"] for c in chunks]
    assert len(chunk_ids) == len(set(chunk_ids))


@pytest.mark.asyncio
async def test_generate_embedding_embed_endpoint():
    mock_embedder = AsyncMock()
    mock_embedder.aembed_query.return_value = [0.1, 0.2, 0.3]
    vec = await generate_embedding("test text", client=mock_embedder)
    assert vec == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_generate_embedding_fallback_endpoint():
    mock_sync = MagicMock(spec=["embed_query"])
    mock_sync.embed_query.return_value = [0.4, 0.5, 0.6]
    vec = await generate_embedding("test text", client=mock_sync)
    assert vec == [0.4, 0.5, 0.6]


@pytest.mark.asyncio
async def test_seed_qdrant_runbooks_flow(tmp_path):
    # Create temporary runbook
    sample_file = tmp_path / "test-runbook.md"
    sample_file.write_text(
        "---\nid: test-rb\nversion: 1\nsymptom: test\ntags: [t]\n---\n\n## Symptom\nTest symptom\n\n## Remediation Steps\nCall `restart_pod`",
        encoding="utf-8",
    )

    created_collections = []
    upserted_points = []

    async def handler(request: httpx.Request):
        # Check collection
        if request.url.path == "/collections/aegis_runbooks" and request.method == "GET":
            return httpx.Response(404)

        # Create collection
        if request.url.path == "/collections/aegis_runbooks" and request.method == "PUT":
            import json
            created_collections.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"status": "ok"})

        # Upsert points
        if request.url.path == "/collections/aegis_runbooks/points":
            import json
            data = json.loads(request.content.decode("utf-8"))
            upserted_points.extend(data["points"])
            return httpx.Response(200, json={"status": "ok"})

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with patch("scripts.seed_runbooks.harness_generate_embedding", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.1, 0.2, 0.3, 0.4]
        async with httpx.AsyncClient(transport=transport) as client:
            count = await seed_qdrant_runbooks(
                config=SystemConfig(),
                runbooks_dir=tmp_path,
                client=client,
            )

    assert count == 2  # Symptom + Remediation Steps
    assert len(created_collections) == 1
    assert created_collections[0]["vectors"]["size"] == 4
    assert len(upserted_points) == 2
    assert upserted_points[1]["payload"]["allowed_actions"] == ["restart_pod"]


def test_lint_rule_document_within_limits():
    meta = {
        "id": "short-rule",
        "title": "Short Rule",
        "types": ["postmortem", "rule"],
        "rules": ["Rule 1: Don't do this.", "Rule 2: Do that."],
        "rule_text": "Rule 1: Don't do this.\nRule 2: Do that.",
    }
    warning = lint_rule_document(meta, [])
    assert warning is None


def test_lint_rule_document_non_rule_ignored():
    meta = {
        "id": "long-procedure",
        "title": "Long Procedure",
        "types": ["runbook", "procedure"],
        "rules": [f"Step {i}: Extensive details here." for i in range(25)],
        "rule_text": "\n".join([f"Step {i}: Extensive details here." for i in range(25)]),
    }
    warning = lint_rule_document(meta, [])
    assert warning is None


def test_lint_rule_document_verbose_rule():
    meta = {
        "id": "oversized-rule",
        "title": "Oversized Rule",
        "types": ["rule"],
        "rules": [f"Rule {i}: Long rule line with multiple words to cause bloating in prompt." for i in range(12)],
        "rule_text": "\n".join([f"Rule {i}: Long rule line with multiple words to cause bloating in prompt." for i in range(12)]),
    }
    warning = lint_rule_document(meta, [])
    assert warning is not None
    assert warning["id"] == "oversized-rule"
    assert warning["rule_count"] == 12
    assert len(warning["reasons"]) >= 1


def test_generate_runbooks_index_linter_skip(tmp_path):
    # One normal runbook, one bloated rule runbook
    normal_rb = tmp_path / "normal.md"
    normal_rb.write_text("---\nid: normal-rb\ntypes: [runbook]\n---\n## Symptom\nNormal symptom", encoding="utf-8")

    bloated_rule = tmp_path / "bloated.md"
    long_rules = "\n".join([f"{i}. Rule number {i} must be strictly adhered to and not violated." for i in range(15)])
    bloated_rule.write_text(f"---\nid: bloated-rule\ntypes: [rule]\n---\n## Operational Rules\n{long_rules}", encoding="utf-8")

    # Reject oversized rule
    index = generate_runbooks_index(
        tmp_path,
        interactive=False,
        confirm_callback=lambda w: False,
    )
    ids = [entry["id"] for entry in index]
    assert "normal-rb" in ids
    assert "bloated-rule" not in ids


def test_generate_runbooks_index_linter_proceed(tmp_path):
    normal_rb = tmp_path / "normal.md"
    normal_rb.write_text("---\nid: normal-rb\ntypes: [runbook]\n---\n## Symptom\nNormal symptom", encoding="utf-8")

    bloated_rule = tmp_path / "bloated.md"
    long_rules = "\n".join([f"{i}. Rule number {i} must be strictly adhered to and not violated." for i in range(15)])
    bloated_rule.write_text(f"---\nid: bloated-rule\ntypes: [rule]\n---\n## Operational Rules\n{long_rules}", encoding="utf-8")

    # Confirm and proceed anyway
    index = generate_runbooks_index(
        tmp_path,
        interactive=False,
        confirm_callback=lambda w: True,
    )
    ids = [entry["id"] for entry in index]
    assert "normal-rb" in ids
    assert "bloated-rule" in ids


def test_extract_frontmatter_lazy(tmp_path):
    from scripts.seed_runbooks import extract_frontmatter_lazy

    test_file = tmp_path / "lazy_test.md"
    # Write a frontmatter block followed by 500 lines of body text
    body = "\n".join([f"Line {i} of large document body that should not be read" for i in range(500)])
    content = f"""---
id: lazy-doc
title: Lazy Stream Test
types: [runbook, procedure]
tags: [streaming, test]
summary: Testing early exit frontmatter parsing
---

## Body Content
{body}
"""
    test_file.write_text(content, encoding="utf-8")

    meta = extract_frontmatter_lazy(test_file)
    assert meta["id"] == "lazy-doc"
    assert meta["title"] == "Lazy Stream Test"
    assert meta["types"] == ["runbook", "procedure"]
    assert meta["tags"] == ["streaming", "test"]
    assert meta["summary"] == "Testing early exit frontmatter parsing"


def test_lint_rule_document_sections_parameter_used():
    # Metadata has no rules, but sections contains operational rules
    meta = {
        "id": "inline-rules-doc",
        "title": "Inline Rules Document",
        "types": ["rule"],
        "rules": [],
        "rule_text": "",
    }
    sections = [
        {
            "section_name": "Symptom",
            "content": "High cpu usage",
        },
        {
            "section_name": "Operational Rules & Policy",
            "content": "\n".join([f"- Rule {i}: This is a verbose rule statement exceeding bounds." for i in range(15)]),
        },
    ]

    warning = lint_rule_document(meta, sections=sections, max_rules=8)
    assert warning is not None
    assert warning["id"] == "inline-rules-doc"
    assert warning["rule_count"] >= 15
    assert any("rule count" in r for r in warning["reasons"])


def test_lint_rule_document_custom_config_boundaries():
    from shared.config.schemas import SAGPoliciesConfig, RunbookLinterConfig

    custom_cfg = SAGPoliciesConfig(
        runbook_linter=RunbookLinterConfig(max_rules=3, max_words=30, max_chars=200, strict=True)
    )
    meta = {
        "id": "custom-bounded-rule",
        "title": "Custom Bounded Rule",
        "types": ["rule"],
        "rules": ["Rule 1", "Rule 2", "Rule 3", "Rule 4"],
        "rule_text": "Rule 1\nRule 2\nRule 3\nRule 4",
    }
    warning = lint_rule_document(meta, config=custom_cfg)
    assert warning is not None
    assert warning["max_rules"] == 3
    assert warning["rule_count"] == 4


@pytest.mark.asyncio
async def test_seed_qdrant_delta_caching(tmp_path):
    from scripts.seed_runbooks import seed_qdrant_runbooks
    import json

    rb_file = tmp_path / "delta-test.md"
    rb_file.write_text(
        "---\nid: delta-rb\nversion: 1\nsymptom: delta test\ntypes: [runbook]\n---\n\n## Symptom\nTest\n\n## Remediation Steps\nCall `restart_pod`",
        encoding="utf-8",
    )

    embed_call_count = 0

    async def mock_embed(*args, **kwargs):
        nonlocal embed_call_count
        embed_call_count += 1
        return [0.1, 0.2, 0.3]

    async def handler(request: httpx.Request):
        if request.url.path == "/collections/aegis_runbooks" and request.method == "GET":
            return httpx.Response(200, json={"result": {"status": "green"}})
        if request.url.path == "/collections/aegis_runbooks/points":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with patch("scripts.seed_runbooks.harness_generate_embedding", side_effect=mock_embed):
        async with httpx.AsyncClient(transport=transport) as client:
            # First seeding run: chunk embeddings should be generated and cached
            count1 = await seed_qdrant_runbooks(
                config=SystemConfig(),
                runbooks_dir=tmp_path,
                client=client,
            )
            assert count1 == 2
            first_run_embed_calls = embed_call_count
            assert first_run_embed_calls == 2

            # Cache file should now exist
            cache_file = tmp_path.parent / ".seed_cache.json"
            assert cache_file.exists()

            # Second seeding run without changes: 0 new embedding calls!
            count2 = await seed_qdrant_runbooks(
                config=SystemConfig(),
                runbooks_dir=tmp_path,
                client=client,
            )
            assert count2 == 2
            # Calls should NOT have increased because all chunks were cached!
            assert embed_call_count == first_run_embed_calls


def test_parse_runbook_section_line_numbers_and_chunks():
    sample_md = """---
id: oom-killed-pod
title: OOMKilled Pod Remediation
---

## Symptom
Container terminated with exit code 137.

## Remediation Steps
1. Call `restart_pod` immediately.
2. Adjust memory limits with `update_resource_limits`.

## Escalation
Page on-call SRE if crash loop persists.
"""
    parsed = parse_runbook_markdown(sample_md)
    secs = parsed["sections"]
    assert len(secs) == 3

    assert secs[0]["section_name"] == "Symptom"
    assert secs[0]["line_start"] == 6
    assert secs[0]["line_end"] == 7
    assert secs[0]["line_range"] == "L6-L7"
    assert secs[0]["chunk_index"] == 1
    assert secs[0]["total_chunks"] == 3
    assert secs[0]["chunk_id"] == "chunk 1/3"

    assert secs[1]["section_name"] == "Remediation Steps"
    assert secs[1]["line_start"] == 9
    assert secs[1]["line_end"] == 11
    assert secs[1]["line_range"] == "L9-L11"
    assert secs[1]["chunk_id"] == "chunk 2/3"

    assert secs[2]["section_name"] == "Escalation"
    assert secs[2]["line_start"] == 13
    assert secs[2]["line_end"] == 14
    assert secs[2]["line_range"] == "L13-L14"
    assert secs[2]["chunk_id"] == "chunk 3/3"


def test_resolve_runbook_section_metadata_otel_demo():
    from services.phase2_worker.runbook_index import resolve_runbook_section_metadata

    meta = resolve_runbook_section_metadata(
        runbook_id="feature-flag-failure",
        section="Remediation Steps",
        namespace="otel-demo",
    )
    assert meta is not None
    assert meta["runbook_id"] == "feature-flag-failure"
    assert "Feature Flag" in meta["book_name"]
    assert meta["section"] == "Remediation Steps"
    assert meta["line_range"].startswith("L")
    assert meta["chunk_id"].startswith("chunk ")
    assert "toggle_feature_flag" in meta["referenced_text"]
