import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Union
from dotenv import load_dotenv
import yaml

from shared.config.schemas import (
    SystemConfig,
    AgentsConfig,
    MCPConfig,
    SAGPoliciesConfig,
    IdentitiesConfig,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"

# Automatically load environment variables from deploy/.env or root .env if present
load_dotenv(PROJECT_ROOT / "deploy" / ".env")
load_dotenv(PROJECT_ROOT / ".env")

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings, dicts, and lists."""
    if isinstance(obj, str):
        def _repl(match: re.Match) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            env_val = os.getenv(var_name)
            if env_val is not None:
                return env_val
            return default_val if default_val is not None else ""

        return _ENV_PATTERN.sub(_repl, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def _load_yaml(path: Union[str, Path]) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_system_config(config_path: Optional[Union[str, Path]] = None) -> SystemConfig:
    path = config_path or os.getenv("AEGIS_SYSTEM_CONFIG") or (DEFAULT_CONFIG_DIR / "system.yaml")
    data = _load_yaml(path)
    config = SystemConfig.model_validate(data)

    # Environment variable overrides
    if os.getenv("REDIS_URL"):
        config.redis.url = os.environ["REDIS_URL"]
    if os.getenv("QDRANT_URL"):
        config.qdrant.url = os.environ["QDRANT_URL"]
    if os.getenv("OLLAMA_URL"):
        config.ollama.url = os.environ["OLLAMA_URL"]
    if os.getenv("TELEGRAM_ENABLED"):
        config.telegram.enabled = os.environ["TELEGRAM_ENABLED"].lower() in ("1", "true", "yes")
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        config.telegram.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_CHAT_ID"):
        config.telegram.chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if os.getenv("TELEGRAM_BOT_URL"):
        config.telegram.bot_url = os.environ["TELEGRAM_BOT_URL"]
    if os.getenv("TELEGRAM_SERVICE_PORT"):
        try:
            config.telegram.service_port = int(os.environ["TELEGRAM_SERVICE_PORT"])
        except ValueError as exc:
            logger.warning("Invalid TELEGRAM_SERVICE_PORT: %s", exc)
    if os.getenv("AEGIS_API_KEY"):
        config.system.api_key = os.environ["AEGIS_API_KEY"]
    if os.getenv("AEGIS_API_URL"):
        config.system.api_url = os.environ["AEGIS_API_URL"]
    if os.getenv("AEGIS_INTERNAL_API_PORT"):
        try:
            config.system.internal_api_port = int(os.environ["AEGIS_INTERNAL_API_PORT"])
        except ValueError as exc:
            logger.warning("Invalid AEGIS_INTERNAL_API_PORT: %s", exc)
    if os.getenv("AEGIS_INTERNAL_API_URL"):
        config.system.internal_api_url = os.environ["AEGIS_INTERNAL_API_URL"]
    if os.getenv("AEGIS_MCP_HOST"):
        config.mcp_server.host = os.environ["AEGIS_MCP_HOST"]
    if os.getenv("AEGIS_MCP_PORT"):
        try:
            config.mcp_server.port = int(os.environ["AEGIS_MCP_PORT"])
        except ValueError as exc:
            logger.warning("Invalid AEGIS_MCP_PORT: %s", exc)
    # Secrets and credentials from environment variables take precedence
    if os.getenv("PROMETHEUS_URL"):
        config.mcp_endpoints.prometheus_url = os.environ["PROMETHEUS_URL"]
    if os.getenv("LOKI_URL"):
        config.mcp_endpoints.loki_url = os.environ["LOKI_URL"]
    if os.getenv("TEMPO_URL"):
        config.mcp_endpoints.tempo_url = os.environ["TEMPO_URL"]
    if os.getenv("JAEGER_URL"):
        config.mcp_endpoints.jaeger_url = os.environ["JAEGER_URL"]
    if os.getenv("FLAGD_URL"):
        config.mcp_endpoints.flagd_url = os.environ["FLAGD_URL"]
    if os.getenv("PROMETHEUS_API_KEY"):
        config.mcp_endpoints.prometheus_api_key = os.environ["PROMETHEUS_API_KEY"]
    if os.getenv("LOKI_API_KEY"):
        config.mcp_endpoints.loki_api_key = os.environ["LOKI_API_KEY"]
    if os.getenv("TEMPO_API_KEY"):
        config.mcp_endpoints.tempo_api_key = os.environ["TEMPO_API_KEY"]
    if os.getenv("JAEGER_API_KEY"):
        config.mcp_endpoints.jaeger_api_key = os.environ["JAEGER_API_KEY"]
    if os.getenv("FLAGD_API_KEY"):
        config.mcp_endpoints.flagd_api_key = os.environ["FLAGD_API_KEY"]
    if os.getenv("KUBERNETES_TOKEN"):
        config.mcp_endpoints.kubernetes_token = os.environ["KUBERNETES_TOKEN"]
    if os.getenv("ARGOCD_TOKEN"):
        config.mcp_endpoints.argocd_token = os.environ["ARGOCD_TOKEN"]
    if os.getenv("GITHUB_TOKEN"):
        config.mcp_endpoints.github_token = os.environ["GITHUB_TOKEN"]
    if os.getenv("AEGIS_API_KEY"):
        config.system.api_key = os.environ["AEGIS_API_KEY"]
    if os.getenv("GOOGLE_API_KEY"):
        config.providers.google_api_key = os.environ["GOOGLE_API_KEY"]
    if os.getenv("OPENAI_API_KEY"):
        config.providers.openai_api_key = os.environ["OPENAI_API_KEY"]
    if os.getenv("ANTHROPIC_API_KEY"):
        config.providers.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        config.telegram.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_CHAT_ID"):
        config.telegram.chat_id = os.environ["TELEGRAM_CHAT_ID"]

    # Explicit environment overrides for container paths / runtime networking if set
    if os.getenv("KUBECONFIG_PATH"):
        config.mcp_endpoints.kubeconfig_path = os.environ["KUBECONFIG_PATH"]
    if os.getenv("AEGIS_STORAGE_PATH"):
        config.ledger.storage_path = os.environ["AEGIS_STORAGE_PATH"]
    if os.getenv("AEGIS_DUMP_IDLE_SECONDS"):
        try:
            config.ledger.dump_idle_seconds = int(os.environ["AEGIS_DUMP_IDLE_SECONDS"])
        except ValueError:
            pass
    if os.getenv("AEGIS_REDIS_TTL_SECONDS"):
        try:
            config.ledger.redis_ttl_seconds = int(os.environ["AEGIS_REDIS_TTL_SECONDS"])
        except ValueError:
            pass
    if os.getenv("AEGIS_SNAPSHOT_PATH"):
        config.topology.snapshot_path = os.environ["AEGIS_SNAPSHOT_PATH"]

    return config


def load_agents_config(config_path: Optional[Union[str, Path]] = None) -> AgentsConfig:
    path = config_path or os.getenv("AEGIS_AGENTS_CONFIG") or (DEFAULT_CONFIG_DIR / "agents.yaml")
    data = _load_yaml(path)
    return AgentsConfig.model_validate(data)


def load_mcp_config(config_path: Optional[Union[str, Path]] = None) -> MCPConfig:
    path = config_path or os.getenv("AEGIS_MCP_CONFIG") or (DEFAULT_CONFIG_DIR / "mcp.yaml")
    raw_data = _load_yaml(path)
    data = _expand_env_vars(raw_data)
    config = MCPConfig.model_validate(data)
    for srv in config.mcp_servers.values():
        if srv.api_key_env and os.getenv(srv.api_key_env):
            srv.api_key = os.environ[srv.api_key_env]
        if srv.api_key == "":
            srv.api_key = None
    if os.getenv("AEGIS_MCP_URL"):
        for srv in config.mcp_servers.values():
            is_http = srv.transport in ("streamable_http", "http")
            is_internal_aegis = "aegis-" in srv.server_url
            is_topology = "9008" in srv.server_url or "topology" in srv.server_url
            if is_http and not srv.external and is_internal_aegis and not is_topology:
                srv.server_url = os.environ["AEGIS_MCP_URL"]
    return config


def load_sag_policies_config(config_path: Optional[Union[str, Path]] = None) -> SAGPoliciesConfig:
    path = config_path or os.getenv("AEGIS_SAG_POLICIES_CONFIG") or (DEFAULT_CONFIG_DIR / "sag_policies.yaml")
    data = _load_yaml(path)
    config = SAGPoliciesConfig.model_validate(data)
    if os.getenv("AEGIS_STRICT_RULE_LINT"):
        val = os.environ["AEGIS_STRICT_RULE_LINT"].lower() in ("1", "true", "yes")
        config.runbook_linter.strict = val
    return config


def load_identities_config(config_path: Optional[Union[str, Path]] = None) -> IdentitiesConfig:
    path = config_path or os.getenv("AEGIS_IDENTITIES_CONFIG") or (DEFAULT_CONFIG_DIR / "identities.yaml")
    data = _load_yaml(path)
    return IdentitiesConfig.model_validate(data)


class ConfigManager:
    def __init__(self, config_dir: Optional[Union[str, Path]] = None):
        base = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.system = load_system_config(base / "system.yaml")
        self.agents = load_agents_config(base / "agents.yaml")
        self.mcp = load_mcp_config(base / "mcp.yaml")
        self.sag = load_sag_policies_config(base / "sag_policies.yaml")
        self.identities = load_identities_config(base / "identities.yaml")


_global_config_instance: Optional[ConfigManager] = None


def get_config(config_dir: Optional[Union[str, Path]] = None, reload: bool = False) -> ConfigManager:
    """Return the global ConfigManager singleton."""
    global _global_config_instance
    if _global_config_instance is None or reload:
        _global_config_instance = ConfigManager(config_dir)
    return _global_config_instance

