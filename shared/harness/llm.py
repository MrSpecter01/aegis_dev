from typing import Optional, List, Any, Union
import os

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI

from shared.config.schemas import AgentLLMConfig, LLMProviderConfig


def _create_single_model(provider_cfg: LLMProviderConfig, ollama_url: Optional[str] = None) -> BaseChatModel:
    provider = provider_cfg.provider.lower()
    model_name = provider_cfg.model
    token_limit = provider_cfg.token_limit
    
    # Common kwargs for all models
    kwargs = {
        "temperature": provider_cfg.temperature,
    }
    if provider_cfg.top_p is not None:
        kwargs["top_p"] = provider_cfg.top_p

    sys_cfg = None
    try:
        from shared.config.loader import ConfigManager
        sys_cfg = ConfigManager().system
    except Exception:
        pass
    providers_cfg = getattr(sys_cfg, "providers", None) if sys_cfg else None

    if provider == "ollama":
        sys_ollama_url = (providers_cfg.ollama_url if providers_cfg and providers_cfg.ollama_url else None) or (sys_cfg.ollama.url if sys_cfg else None)
        base_url = provider_cfg.base_url or ollama_url or os.getenv("OLLAMA_URL") or sys_ollama_url
        if token_limit > 0:
            kwargs["num_predict"] = token_limit
        kwargs["base_url"] = base_url
        return ChatOllama(model=model_name, **kwargs)
        
    elif provider == "openai":
        api_key = provider_cfg.api_key or (providers_cfg.openai_api_key if providers_cfg else None) or os.getenv("OPENAI_API_KEY")
        if token_limit > 0:
            kwargs["max_tokens"] = token_limit
        return ChatOpenAI(
            model=model_name, 
            api_key=api_key, 
            base_url=provider_cfg.base_url or os.getenv("OPENAI_BASE_URL"), 
            **kwargs
        )
        
    elif provider == "gemini":
        api_key = provider_cfg.api_key or (providers_cfg.google_api_key if providers_cfg else None) or os.getenv("GOOGLE_API_KEY")
        if token_limit > 0:
            kwargs["max_output_tokens"] = token_limit
        return ChatGoogleGenerativeAI(
            model=model_name,
            api_key=api_key,
            **kwargs
        )
        
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


def build_llm(agent_config: Union[AgentLLMConfig, str], ollama_url: Optional[str] = None) -> BaseChatModel:
    """Builds a primary ChatModel with optional fallback chain attached.

    Accepts either an AgentLLMConfig instance or an agent_id string (e.g. 'infrastructure_investigator').
    Token limits from agent config are enforced at model construction time via
    provider-specific parameters (num_predict for Ollama, max_tokens for OpenAI).
    """
    if isinstance(agent_config, str):
        from shared.config.loader import load_agents_config
        agents_cfg = load_agents_config()
        if agent_config not in agents_cfg.agents:
            raise KeyError(f"Agent '{agent_config}' not found in agents.yaml config")
        agent_config = agents_cfg.agents[agent_config]

    primary_model = _create_single_model(agent_config.primary, ollama_url=ollama_url)

    if not agent_config.fallback:
        return primary_model

    fallbacks: List[BaseChatModel] = []
    for fb_cfg in agent_config.fallback:
        try:
            fallbacks.append(_create_single_model(fb_cfg, ollama_url=ollama_url))
        except Exception as exc:
            # Skip invalid fallback configurations gracefully
            logger.warning("Failed to initialize fallback model %s: %s", getattr(fb_cfg, "model", fb_cfg), exc, exc_info=True)

    if fallbacks:
        return primary_model.with_fallbacks(fallbacks)
    return primary_model


class TokenBudgetExceeded(Exception):
    """Raised when an agent invocation exceeds its allocated token budget."""

    def __init__(self, message: str = "Token budget exceeded", tokens_used: int = 0, budget: int = 0):
        super().__init__(message)
        self.tokens_used = tokens_used
        self.budget = budget


class TokenBudgetCallbackHandler(BaseCallbackHandler):
    """Monitors token usage across LLM calls and raises TokenBudgetExceeded if limit hit."""

    def __init__(self, token_budget: int):
        self.token_budget = token_budget
        self.tokens_used = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        usage = getattr(response, "llm_output", None) or {}
        token_usage = usage.get("token_usage", {})
        total_tokens = token_usage.get("total_tokens", 0)
        self.tokens_used += total_tokens
        if self.token_budget > 0 and self.tokens_used > self.token_budget:
            raise TokenBudgetExceeded(
                f"Token budget of {self.token_budget} exceeded (used {self.tokens_used})",
                tokens_used=self.tokens_used,
                budget=self.token_budget,
            )

