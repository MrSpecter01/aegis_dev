import logging
import os
from typing import Any, List, Optional

from shared.config import ConfigManager

logger = logging.getLogger(__name__)


def build_embeddings(
    provider: str = "ollama",
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Build and return a LangChain Embeddings instance dynamically based on configuration."""
    clean_provider = provider.lower().strip()
    sys_cfg = None
    try:
        sys_cfg = ConfigManager().system
    except Exception as exc:
        logger.warning("Could not load ConfigManager in build_embeddings: %s", exc)

    if clean_provider == "ollama":
        from langchain_ollama import OllamaEmbeddings
        fallback_url = sys_cfg.ollama.url if sys_cfg else None
        fallback_model = sys_cfg.qdrant.embedding_model if sys_cfg else None
        raw_url = base_url or os.getenv("OLLAMA_URL") or fallback_url
        embed_model = model or os.getenv("EMBEDDING_MODEL") or fallback_model
        if not raw_url or not embed_model:
            raise ValueError("Ollama URL and embedding model must be configured in system config or environment")
        return OllamaEmbeddings(base_url=raw_url.rstrip("/"), model=embed_model)

    elif clean_provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY is required for openai embeddings")
        fallback_url = sys_cfg.embeddings.openai_base_url if sys_cfg else None
        fallback_model = sys_cfg.embeddings.openai_model if sys_cfg else None
        raw_url = base_url or os.getenv("OPENAI_BASE_URL") or fallback_url
        embed_model = model or os.getenv("EMBEDDING_MODEL") or fallback_model
        if not raw_url or not embed_model:
            raise ValueError("OpenAI embedding URL and model must be configured in system config or environment")
        return OpenAIEmbeddings(openai_api_key=key, base_url=raw_url.rstrip("/"), model=embed_model)

    elif clean_provider in ("gemini", "google"):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            raise ValueError("GOOGLE_API_KEY is required for gemini embeddings")
        fallback_model = sys_cfg.embeddings.gemini_model if sys_cfg else None
        embed_model = model or os.getenv("EMBEDDING_MODEL") or fallback_model
        if not embed_model:
            raise ValueError("Gemini embedding model must be configured in system config or environment")
        model_name = embed_model if embed_model.startswith("models/") else f"models/{embed_model}"
        return GoogleGenerativeAIEmbeddings(google_api_key=key, model=model_name)

    else:
        raise ValueError(f"Unsupported embedding provider: '{provider}'. Supported: 'ollama', 'openai', 'gemini'.")


async def generate_embedding(
    text: str,
    provider: str = "ollama",
    model: Optional[str] = None,
    client: Optional[Any] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[float]:
    """Generate vector embedding for a given text string using LangChain Embeddings.

    If a custom embedder `client` implementing `aembed_query` or `embed_query` is provided,
    it is used directly. Otherwise, a LangChain Embeddings instance is constructed via `build_embeddings()`.
    """
    clean_provider = provider.lower().strip()

    # 1. Custom LangChain Embeddings or mock embedder provided
    if client is not None:
        if hasattr(client, "aembed_query"):
            return await client.aembed_query(text)
        if hasattr(client, "embed_query"):
            return client.embed_query(text)

    # 2. Build and use LangChain Embeddings instance
    embedder = build_embeddings(provider=clean_provider, model=model, base_url=base_url, api_key=api_key)
    if hasattr(embedder, "aembed_query"):
        return await embedder.aembed_query(text)
    if hasattr(embedder, "embed_query"):
        return embedder.embed_query(text)

    raise TypeError(f"Embedder {type(embedder)} must implement aembed_query or embed_query")
