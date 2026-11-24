import pytest
import httpx
from unittest.mock import AsyncMock, patch
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.qdrant import (
    validate_qdrant_query,
    validate_collection_name,
    validate_top_k,
    generate_query_embedding,
    qdrant_search,
    register_tools,
)


def test_validate_qdrant_query():
    # Valid query
    validate_qdrant_query("OOMKilled pods in cartservice")
    # Valid id only
    validate_qdrant_query(None, id_param="oom-killed-pod")

    # Both empty
    with pytest.raises(ValueError, match="Either 'query' or 'id' must be provided"):
        validate_qdrant_query("")
    with pytest.raises(ValueError, match="Either 'query' or 'id' must be provided"):
        validate_qdrant_query(None, id_param=None)

    # Empty whitespace query with no id
    with pytest.raises(ValueError, match="cannot be empty whitespace"):
        validate_qdrant_query("   ")

    # Oversized query
    with pytest.raises(ValueError, match="exceeds maximum length"):
        validate_qdrant_query("a" * 1001)


def test_validate_collection_name():
    # Valid
    validate_collection_name("aegis_runbooks")
    validate_collection_name("runbooks-v1")

    # Empty
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_collection_name("")

    # Invalid chars / path traversal
    with pytest.raises(ValueError, match="Invalid collection name"):
        validate_collection_name("../bad/path")
    with pytest.raises(ValueError, match="Invalid collection name"):
        validate_collection_name("coll; DROP TABLE;")


def test_validate_top_k():
    assert validate_top_k(1) == 1
    assert validate_top_k(5) == 5
    assert validate_top_k(20) == 20

    with pytest.raises(ValueError, match="between 1 and 20"):
        validate_top_k(0)
    with pytest.raises(ValueError, match="between 1 and 20"):
        validate_top_k(21)


@pytest.mark.asyncio
async def test_generate_query_embedding():
    mock_embedder = AsyncMock()
    mock_embedder.aembed_query.return_value = [0.5, 0.6, 0.7]
    vec = await generate_query_embedding("query", client=mock_embedder)
    assert vec == [0.5, 0.6, 0.7]


@pytest.mark.asyncio
async def test_qdrant_search_happy_path():
    captured_payloads = []

    async def handler(request: httpx.Request):
        if request.url.path == "/collections/aegis_runbooks/points/search":
            import json
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": "point-1",
                            "score": 0.92,
                            "payload": {
                                "runbook_id": "oom-killed-pod",
                                "title": "OOMKilled Pod Remediation",
                                "version": 1,
                                "types": ["runbook", "procedure"],
                                "section": "Remediation Steps",
                                "symptom": "OOMKilled pods",
                                "tags": ["memory", "oom"],
                                "content": "1. Restart pod: `restart_pod`",
                                "allowed_actions": ["restart_pod"],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with patch("services.mcp.tools.qdrant.generate_embedding", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.1, 0.2, 0.3]
        async with httpx.AsyncClient(transport=transport) as client:
            result = await qdrant_search(
                query="cartservice memory saturated OOM",
                doc_type="procedure",
                collection="aegis_runbooks",
                top_k=3,
                client=client,
                qdrant_url="http://localhost:6333",
            )

    assert result["query"] == "cartservice memory saturated OOM"
    assert result["doc_type"] == "procedure"
    assert result["collection"] == "aegis_runbooks"
    assert result["result_count"] == 1
    top_res = result["results"][0]
    assert top_res["runbook_id"] == "oom-killed-pod"
    assert top_res["types"] == ["runbook", "procedure"]
    assert "restart_pod" in top_res["allowed_actions"]

    # Verify filter sent to Qdrant
    assert len(captured_payloads) == 1
    assert "filter" in captured_payloads[0]
    assert captured_payloads[0]["filter"]["must"][0] == {"key": "types", "match": {"value": "procedure"}}


@pytest.mark.asyncio
async def test_qdrant_search_exact_id_lookup():
    captured_payloads = []

    async def handler(request: httpx.Request):
        if request.url.path == "/collections/aegis_runbooks/points/scroll":
            import json
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": "point-core-1",
                                "payload": {
                                    "runbook_id": "pm-2025-04-coredns",
                                    "title": "CoreDNS Failure Postmortem",
                                    "types": ["postmortem", "rule"],
                                    "section": "Operational Rules",
                                    "content": "Never drain CoreDNS nodes simultaneously",
                                    "allowed_actions": ["scale_deployment"],
                                },
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await qdrant_search(
            id="pm-2025-04-coredns",
            collection="aegis_runbooks",
            top_k=2,
            client=client,
            qdrant_url="http://localhost:6333",
        )

    assert result["id"] == "pm-2025-04-coredns"
    assert result["result_count"] == 1
    assert result["results"][0]["runbook_id"] == "pm-2025-04-coredns"
    assert result["results"][0]["types"] == ["postmortem", "rule"]
    assert len(captured_payloads) == 1
    assert captured_payloads[0]["filter"]["must"][0] == {"key": "runbook_id", "match": {"value": "pm-2025-04-coredns"}}


@pytest.mark.asyncio
async def test_qdrant_search_http_error():
    async def handler(request: httpx.Request):
        if request.url.path == "/collections/aegis_runbooks/points/search":
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with patch("services.mcp.tools.qdrant.generate_embedding", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.1, 0.2, 0.3]
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(RuntimeError, match="Qdrant HTTP error 500"):
                await qdrant_search(
                    query="test query",
                    client=client,
                    qdrant_url="http://localhost:6333",
                )


def test_register_tools_registers_qdrant_search():
    app = FastMCP("test-mcp")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "qdrant_search" in tools


@pytest.mark.asyncio
async def test_qdrant_search_tag_and_section_filters():
    captured_payloads = []

    async def handler(request: httpx.Request):
        if request.url.path == "/collections/aegis_runbooks/points/scroll":
            import json
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": "point-sec-1",
                                "payload": {
                                    "runbook_id": "payment-failure",
                                    "title": "Payment Service Outage",
                                    "tags": ["paymentservice", "checkout"],
                                    "section": "Immediate Mitigation",
                                    "content": "Toggle fallback payment provider",
                                    "allowed_actions": ["toggle_feature_flag"],
                                },
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await qdrant_search(
            id="payment-failure",
            tag="paymentservice",
            section="Immediate Mitigation",
            client=client,
            qdrant_url="http://localhost:6333",
        )

    assert result["tag"] == "paymentservice"
    assert result["section"] == "Immediate Mitigation"
    assert len(captured_payloads) == 1
    filters = captured_payloads[0]["filter"]["must"]
    filter_keys = {f["key"]: f["match"]["value"] for f in filters}
    assert filter_keys["runbook_id"] == "payment-failure"
    assert filter_keys["tags"] == "paymentservice"
    assert filter_keys["section"] == "Immediate Mitigation"


@pytest.mark.asyncio
async def test_qdrant_search_preserves_line_and_chunk_metadata():
    async def handler(request: httpx.Request):
        if request.url.path == "/collections/aegis_runbooks/points/search":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": "pt-meta-1",
                            "score": 0.95,
                            "payload": {
                                "runbook_id": "oom-killed-pod",
                                "title": "OOMKilled Pod Remediation",
                                "section": "Remediation Steps",
                                "line_start": 18,
                                "line_end": 26,
                                "line_range": "L18-L26",
                                "chunk_index": 2,
                                "total_chunks": 4,
                                "chunk_label": "chunk 2/4",
                                "content": "Call update_resource_limits to bump memory limits.",
                                "allowed_actions": ["update_resource_limits"],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with patch("services.mcp.tools.qdrant.generate_embedding", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.1, 0.2]
        async with httpx.AsyncClient(transport=transport) as client:
            result = await qdrant_search(
                query="oom memory limit",
                client=client,
                qdrant_url="http://localhost:6333",
            )

    item = result["results"][0]
    assert item["line_start"] == 18
    assert item["line_end"] == 26
    assert item["line_range"] == "L18-L26"
    assert item["chunk_index"] == 2
    assert item["total_chunks"] == 4
    assert item["chunk_id"] == "chunk 2/4"
