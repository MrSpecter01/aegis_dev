import os
import re
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.config.loader import ConfigManager
from shared.harness.embeddings import generate_embedding

COLLECTION_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")


def validate_qdrant_query(query: Optional[str], id_param: Optional[str] = None) -> None:
    """Validate search query and id parameters to ensure at least one is provided and bounded."""
    if not query and not id_param:
        raise ValueError("Either 'query' or 'id' must be provided for qdrant_search")
    if query:
        if not query.strip():
            raise ValueError("Search query cannot be empty whitespace")
        if len(query) > 1000:
            raise ValueError("Search query exceeds maximum length of 1000 characters")


def validate_collection_name(collection: str) -> None:
    """Validate Qdrant collection name to prevent path traversal or malformed endpoints."""
    if not collection or not collection.strip():
        raise ValueError("Collection name cannot be empty")
    if not COLLECTION_NAME_PATTERN.match(collection.strip()):
        raise ValueError(
            f"Invalid collection name: '{collection}'. Allowed characters: alphanumeric, underscores, hyphens"
        )


def validate_top_k(top_k: int) -> int:
    """Clamp or validate top_k parameter."""
    if top_k < 1 or top_k > 20:
        raise ValueError("top_k must be between 1 and 20")
    return top_k


async def generate_query_embedding(
    text: str,
    ollama_url: Optional[str] = None,
    model: Optional[str] = None,
    client: Optional[Any] = None,
    provider: Optional[str] = None,
) -> List[float]:
    """Request vector embedding for a query string using the harness."""
    config = ConfigManager()
    active_provider = provider or config.system.qdrant.embedding_provider
    active_model = model or config.system.qdrant.embedding_model
    return await generate_embedding(
        text=text,
        provider=active_provider,
        model=active_model,
        client=client,
        base_url=ollama_url or (config.system.ollama.url if active_provider == "ollama" else None),
    )


async def qdrant_search(
    query: Optional[str] = None,
    doc_type: Optional[str] = None,
    id: Optional[str] = None,
    tag: Optional[str] = None,
    section: Optional[str] = None,
    collection: Optional[str] = None,
    top_k: int = 3,
    client: Optional[httpx.AsyncClient] = None,
    qdrant_url: Optional[str] = None,
    ollama_url: Optional[str] = None,
    embedding_model: Optional[str] = None,
    embedding_provider: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search against Qdrant vector database for runbook chunks and prior incidents.
    Supports semantic vector search (when query is provided), exact lookup (by id),
    and metadata filtering (by doc_type, tag, or section).
    """
    config = ConfigManager()
    default_collection = config.system.qdrant.collection or "aegis_runbooks"
    collection = collection or default_collection
    validate_qdrant_query(query, id_param=id)
    validate_collection_name(collection)
    top_k = validate_top_k(top_k)

    base_qdrant_url = qdrant_url or os.getenv("QDRANT_URL") or config.system.qdrant.url
    clean_qdrant_url = base_qdrant_url.rstrip("/")

    base_ollama_url = ollama_url or os.getenv("OLLAMA_URL") or config.system.ollama.url
    model = embedding_model or config.system.qdrant.embedding_model
    provider = embedding_provider or config.system.qdrant.embedding_provider

    # Build Qdrant filter conditions
    must_conditions: List[Dict[str, Any]] = []
    if doc_type:
        must_conditions.append({
            "key": "types",
            "match": {"value": doc_type.strip().lower()},
        })
    if id:
        must_conditions.append({
            "key": "runbook_id",
            "match": {"value": id.strip()},
        })
    if tag:
        must_conditions.append({
            "key": "tags",
            "match": {"value": tag.strip().lower()},
        })
    if section:
        must_conditions.append({
            "key": "section",
            "match": {"value": section.strip()},
        })

    filter_dict = {"must": must_conditions} if must_conditions else None

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        raw_results: List[Dict[str, Any]] = []

        if query:
            # 1. Semantic search with embedding
            query_vector = await generate_embedding(
                text=query,
                provider=provider,
                model=model,
                base_url=base_ollama_url if provider == "ollama" else None,
            )
            endpoint = f"{clean_qdrant_url}/collections/{collection}/points/search"
            payload: Dict[str, Any] = {
                "vector": query_vector,
                "limit": top_k,
                "with_payload": True,
            }
            if filter_dict:
                payload["filter"] = filter_dict

            try:
                resp = await c.post(endpoint, json=payload, timeout=15.0)
                if resp.status_code == 404 and collection != default_collection:
                    endpoint = f"{clean_qdrant_url}/collections/{default_collection}/points/search"
                    resp = await c.post(endpoint, json=payload, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
                raw_results = data.get("result", [])
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Qdrant HTTP error {e.response.status_code}: {e.response.text}") from e
            except httpx.RequestError as e:
                raise RuntimeError(f"Qdrant connection error: {str(e)}") from e

        else:
            # 2. Exact match scroll query without embedding
            endpoint = f"{clean_qdrant_url}/collections/{collection}/points/scroll"
            payload = {
                "limit": top_k,
                "with_payload": True,
            }
            if filter_dict:
                payload["filter"] = filter_dict

            try:
                resp = await c.post(endpoint, json=payload, timeout=15.0)
                if resp.status_code == 404 and collection != default_collection:
                    endpoint = f"{clean_qdrant_url}/collections/{default_collection}/points/scroll"
                    resp = await c.post(endpoint, json=payload, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
                raw_results = data.get("result", {}).get("points", [])
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"Qdrant HTTP error {e.response.status_code}: {e.response.text}") from e
            except httpx.RequestError as e:
                raise RuntimeError(f"Qdrant connection error: {str(e)}") from e

        formatted_results: List[Dict[str, Any]] = []
        for item in raw_results:
            point_payload = item.get("payload", {})
            runbook_id_val = point_payload.get("runbook_id", "")
            section_val = point_payload.get("section", "")
            line_start_val = point_payload.get("line_start")
            line_end_val = point_payload.get("line_end")
            line_range_val = point_payload.get("line_range") or ""
            if not line_range_val and line_start_val is not None and line_end_val is not None:
                line_range_val = f"L{line_start_val}-L{line_end_val}"
            chunk_idx_val = point_payload.get("chunk_index")
            total_chunks_val = point_payload.get("total_chunks")
            chunk_id_val = point_payload.get("chunk_label") or point_payload.get("chunk_id") or ""
            if not chunk_id_val and chunk_idx_val is not None and total_chunks_val is not None:
                chunk_id_val = f"chunk {chunk_idx_val}/{total_chunks_val}"

            # Backfill line numbers / chunk label on the fly if Qdrant point was seeded before metadata enrichment
            if (not line_range_val or not chunk_id_val) and runbook_id_val:
                try:
                    from services.phase2_worker.runbook_index import resolve_runbook_section_metadata
                    ns_hint = None
                    if collection and collection.startswith("aegis_runbooks_"):
                        ns_hint = collection[len("aegis_runbooks_"):].replace("_", "-")
                    resolved = resolve_runbook_section_metadata(
                        runbook_id=runbook_id_val,
                        section=section_val,
                        namespace=ns_hint,
                    )
                    if resolved:
                        line_range_val = line_range_val or resolved.get("line_range", "")
                        chunk_id_val = chunk_id_val or resolved.get("chunk_id", "")
                except Exception:
                    pass

            formatted_results.append({
                "id": item.get("id"),
                "score": item.get("score", 1.0),
                "runbook_id": runbook_id_val,
                "title": point_payload.get("title", ""),
                "version": point_payload.get("version", 1),
                "types": point_payload.get("types", ["runbook"]),
                "section": section_val,
                "line_start": line_start_val,
                "line_end": line_end_val,
                "line_range": line_range_val,
                "chunk_index": chunk_idx_val,
                "total_chunks": total_chunks_val,
                "chunk_id": chunk_id_val,
                "symptoms": point_payload.get("symptoms", []),
                "tags": point_payload.get("tags", []),
                "summary": point_payload.get("summary", ""),
                "content": point_payload.get("content", ""),
                "allowed_actions": point_payload.get("allowed_actions", []),
            })

        return {
            "query": query,
            "doc_type": doc_type,
            "id": id,
            "tag": tag,
            "section": section,
            "collection": collection,
            "result_count": len(formatted_results),
            "results": formatted_results,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register Qdrant vector search tool onto a FastMCP instance."""
    @mcp.tool(name="qdrant_search")
    async def mcp_qdrant_search(
        query: Optional[str] = None,
        doc_type: Optional[str] = None,
        id: Optional[str] = None,
        tag: Optional[str] = None,
        section: Optional[str] = None,
        collection: Optional[str] = None,
        top_k: int = 3,
    ) -> Dict[str, Any]:
        """
        Search Qdrant runbook knowledge base.
        Accepts semantic query, exact doc id, doc_type filter (e.g. 'runbook', 'procedure', 'postmortem', 'rule'),
        service tag (e.g. 'auth-service', 'api-gateway'), or section title (e.g. 'Immediate Mitigation').
        """
        return await qdrant_search(
            query=query,
            doc_type=doc_type,
            id=id,
            tag=tag,
            section=section,
            collection=collection,
            top_k=top_k,
        )
