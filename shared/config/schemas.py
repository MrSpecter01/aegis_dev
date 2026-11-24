from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


class LLMProviderConfig(BaseModel):
    provider: str  # ollama, openai, gemini
    model: str
    token_limit: int = 4000
    temperature: float = 0.0
    top_p: Optional[float] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None


class AgentLLMConfig(BaseModel):
    primary: LLMProviderConfig
    fallback: List[LLMProviderConfig] = Field(default_factory=list)
    timeout_ms: int = 30000
    retry_on: List[str] = Field(default_factory=lambda: ["rate_limit", "timeout", "provider_error"])
    token_budget: Optional[int] = None
    max_iter: Optional[int] = None
    max_tool_calls: Optional[int] = None
    tool_selector: Optional[Dict[str, List[str]]] = None



class SystemConfigDetails(BaseModel):
    environment: str = "development"
    log_level: str = "INFO"
    api_port: int = 8080
    api_key: str = ""
    api_url: str = "http://localhost:8080"
    internal_api_port: int = 8081
    internal_api_url: str = "http://localhost:8081"



class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379"
    db: int = 0


class QdrantConfig(BaseModel):
    url: str = "http://localhost:6333"
    collection: str = "aegis_runbooks"
    embedding_provider: str = "ollama"
    embedding_model: str = "qwen3-embedding:0.6b-q8_0"
    catalog_mode: Literal["auto", "direct", "two_step"] = "auto"
    catalog_inline_max_entries: int = 15


class ConvergenceSLOConfig(BaseModel):
    error_rate_threshold: float = 0.01
    p99_latency_ms: float = 500.0
    cpu_saturation_threshold: float = 0.85
    memory_saturation_threshold: float = 0.90


class ConvergenceQueriesConfig(BaseModel):
    error_rate: str = 'sum(rate(http_requests_total{job=~"{service}.*",status=~"5.."}[2m])) / sum(rate(http_requests_total{job=~"{service}.*"}[2m]))'
    p99_latency_ms: str = 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{job=~"{service}.*"}[2m])) by (le)) * 1000'
    cpu_saturation: str = 'sum(rate(container_cpu_usage_seconds_total{pod=~"{service}.*"}[2m])) / (sum(container_spec_cpu_quota{pod=~"{service}.*"}) / sum(container_spec_cpu_period{pod=~"{service}.*"}))'
    memory_saturation: str = 'sum(container_memory_working_set_bytes{pod=~"{service}.*"}) / sum(container_spec_memory_limit_bytes{pod=~"{service}.*"})'


class ConvergenceConfig(BaseModel):
    bake_time_seconds_stateless: int = 60
    bake_time_seconds_stateful: int = 120
    poll_interval_seconds: int = 30
    window_seconds: int = 300
    slo: ConvergenceSLOConfig = Field(default_factory=ConvergenceSLOConfig)
    queries: ConvergenceQueriesConfig = Field(default_factory=ConvergenceQueriesConfig)


class OllamaConfig(BaseModel):
    url: str = "http://localhost:11434"


class TelegramConfig(BaseModel):
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    bot_url: str = "http://localhost:8088"
    service_port: int = 8088
    notify_on_creation: bool = True
    notify_on_triage: bool = True
    notify_on_approval: bool = True
    notify_on_escalation: bool = True
    notify_on_closure: bool = True


class LedgerConfig(BaseModel):
    dump_on_close: bool = True
    dump_idle_seconds: int = 1800
    redis_ttl_seconds: int = 86400
    storage_path: str = "/data/incidents"


class IngestionConfig(BaseModel):
    correlation_window_seconds: int = 180
    flap_window_seconds: int = 60
    flap_threshold: int = 5
    recurrence_window_seconds: int = 900
    recurrence_threshold: int = 3
    topology_max_hops: int = 2
    consumer_group: str = "aegis-correlator"
    consumer_name: str = "consumer-1"
    severity_map: Dict[str, str] = Field(
        default_factory=lambda: {"critical": "P1", "warning": "P2", "info": "P3"}
    )
    fingerprint_ttl_seconds: int = 3600
    max_delivery_attempts: int = 3
    allowed_namespaces: List[str] = Field(default_factory=list)


class TopologyConfig(BaseModel):
    helm_dir: str = "./charts"
    snapshot_path: str = "/data/topology_snapshot.json"
    query_budget_per_incident: int = 5
    monitored_namespaces: List[str] = Field(default_factory=lambda: ["boutique", "default"])


class McpEndpointsConfig(BaseModel):
    prometheus_url: str = "http://localhost:9090"
    prometheus_api_key: str = ""
    prometheus_auth_type: Literal["bearer", "api_key", "header", "basic", "none"] = "bearer"
    prometheus_auth_header: str = ""
    loki_url: str = "http://localhost:3100"
    loki_api_key: str = ""
    loki_auth_type: Literal["bearer", "api_key", "header", "basic", "none"] = "bearer"
    loki_auth_header: str = ""
    tempo_url: str = "http://localhost:3200"
    tempo_api_key: str = ""
    tempo_auth_type: Literal["bearer", "api_key", "header", "basic", "none"] = "bearer"
    tempo_auth_header: str = ""
    jaeger_url: str = ""
    jaeger_api_key: str = ""
    jaeger_auth_type: Literal["bearer", "api_key", "header", "basic", "none"] = "bearer"
    jaeger_auth_header: str = ""
    flagd_url: str = ""
    flagd_api_key: str = ""
    flagd_auth_type: Literal["bearer", "api_key", "header", "basic", "none"] = "bearer"
    flagd_auth_header: str = ""
    kubernetes_url: str = ""
    kubernetes_token: str = ""
    kubeconfig_path: str = ""
    argocd_url: str = ""
    argocd_token: str = ""
    github_token: str = ""


class MCPServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9000


class EmbeddingsConfig(BaseModel):
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "text-embedding-3-small"
    gemini_model: str = "text-embedding-004"


class ProvidersConfig(BaseModel):
    google_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    ollama_url: Optional[str] = None


class EvaluationJudgeConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-3.5-flash-lite"
    fallbacks: List[str] = Field(
        default_factory=lambda: ["gemini-3.1-flash-lite", "gemma-4-31b"]
    )
    api_key: Optional[str] = None
    max_calls_per_run: int = 50
    rate_limit_rpm: int = 15
    temperature: float = 0.0


class EvaluationConfig(BaseModel):
    judge: EvaluationJudgeConfig = Field(default_factory=EvaluationJudgeConfig)


class SystemConfig(BaseModel):
    system: SystemConfigDetails = Field(default_factory=SystemConfigDetails)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    topology: TopologyConfig = Field(default_factory=TopologyConfig)
    mcp_endpoints: McpEndpointsConfig = Field(default_factory=McpEndpointsConfig)
    convergence: ConvergenceConfig = Field(default_factory=ConvergenceConfig)
    mcp_server: MCPServerSettings = Field(default_factory=MCPServerSettings)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)



class AgentsConfig(BaseModel):
    agents: Dict[str, AgentLLMConfig] = Field(default_factory=dict)


class ToolItemConfig(BaseModel):
    name: str
    tags: List[str] = Field(default_factory=list)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.name == other
        if isinstance(other, ToolItemConfig):
            return self.name == other.name and self.tags == other.tags
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(self.name)

    def __str__(self) -> str:
        return self.name


class MCPServerConfig(BaseModel):
    server_url: str = ""
    transport: Literal["streamable_http", "http", "sse", "stdio"] = "streamable_http"
    tools: List[Union[str, ToolItemConfig]] = Field(default_factory=list)
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    auth_type: Literal["bearer", "api_key", "header", "basic", "none"] = "bearer"
    auth_header: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: Optional[str] = None
    timeout: float = 30.0
    external: bool = False

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerConfig":
        if self.transport in ("streamable_http", "http", "sse") and not self.server_url:
            raise ValueError(f"server_url is required when transport is '{self.transport}'")
        if self.transport == "stdio" and not self.command:
            raise ValueError("command is required when transport is 'stdio'")
        return self

    @property
    def tool_names(self) -> List[str]:
        names: List[str] = []
        for t in self.tools:
            if isinstance(t, ToolItemConfig):
                names.append(t.name)
            elif isinstance(t, dict):
                names.append(t.get("name", ""))
            else:
                names.append(str(t))
        return names

    def get_tool_tags(self, tool_name: str) -> List[str]:
        for t in self.tools:
            if isinstance(t, ToolItemConfig) and t.name == tool_name:
                return t.tags
            elif isinstance(t, dict) and t.get("name") == tool_name:
                return t.get("tags", [])
        return []

    def __contains__(self, item: Any) -> bool:
        return item in self.tool_names


class MCPConfig(BaseModel):
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=dict)


class ToolPolicy(BaseModel):
    pre_approved: bool = False
    requires_approval: bool = True
    allowed_namespaces: List[str] = Field(default_factory=list)
    max_replicas: Optional[int] = None
    max_cpu_increase_ratio: Optional[float] = None
    max_memory_increase_ratio: Optional[float] = None
    strict_confirmation: Optional[bool] = None
    severity_override: Optional[bool] = None
    allowed_environments: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)


class RunbookLinterConfig(BaseModel):
    max_rules: int = 8
    max_words: int = 150
    max_chars: int = 800
    strict: bool = False


class SAGPoliciesConfig(BaseModel):
    policies: Dict[str, Any] = Field(default_factory=dict)
    default_requires_approval: bool = True
    allowed_namespaces: List[str] = Field(default_factory=list)
    tools: Dict[str, ToolPolicy] = Field(default_factory=dict)
    confidence_thresholds: Dict[str, float] = Field(
        default_factory=lambda: {"P1": 0.90, "P2": 0.80, "P3": 0.70}
    )
    max_retries: Dict[str, int] = Field(
        default_factory=lambda: {"P1": 2, "P2": 3, "P3": 5}
    )
    approval_timeout_seconds: Union[int, Dict[str, int]] = Field(
        default_factory=lambda: {"P1": 300, "P2": 900, "P3": 1800}
    )
    record_retention_seconds: int = 3600
    timeout_behavior: str = "escalate"
    runbook_linter: RunbookLinterConfig = Field(default_factory=RunbookLinterConfig)

    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def populate_from_policies(cls, data: Any) -> Any:
        if isinstance(data, dict):
            policies_dict = data.get("policies", {})
            if isinstance(policies_dict, dict):
                if "default_requires_approval" in policies_dict and "default_requires_approval" not in data:
                    data["default_requires_approval"] = policies_dict["default_requires_approval"]
                if "allowed_namespaces" in policies_dict and "allowed_namespaces" not in data:
                    data["allowed_namespaces"] = policies_dict["allowed_namespaces"]
                if "tools" in policies_dict and "tools" not in data:
                    data["tools"] = policies_dict["tools"]
                if "confidence_thresholds" in policies_dict and "confidence_thresholds" not in data:
                    data["confidence_thresholds"] = policies_dict["confidence_thresholds"]
                if "max_retries" in policies_dict and "max_retries" not in data:
                    data["max_retries"] = policies_dict["max_retries"]
                if "approval_timeout_seconds" in policies_dict and "approval_timeout_seconds" not in data:
                    data["approval_timeout_seconds"] = policies_dict["approval_timeout_seconds"]
                if "record_retention_seconds" in policies_dict and "record_retention_seconds" not in data:
                    data["record_retention_seconds"] = policies_dict["record_retention_seconds"]
                if "timeout_behavior" in policies_dict and "timeout_behavior" not in data:
                    data["timeout_behavior"] = policies_dict["timeout_behavior"]
                if "runbook_linter" in policies_dict and "runbook_linter" not in data:
                    data["runbook_linter"] = policies_dict["runbook_linter"]
        return data

    def get_tool_policy(self, tool_name: str) -> ToolPolicy:
        if tool_name in self.tools:
            return self.tools[tool_name]
        tools_dict = self.policies.get("tools", {})
        if tool_name in tools_dict:
            raw = tools_dict[tool_name]
            return ToolPolicy.model_validate(raw) if isinstance(raw, dict) else raw
        return ToolPolicy(requires_approval=self.default_requires_approval)

    def get_timeout_seconds(self, severity: str) -> int:
        if isinstance(self.approval_timeout_seconds, dict):
            return self.approval_timeout_seconds.get(severity, self.approval_timeout_seconds.get("P2", 900))
        return int(self.approval_timeout_seconds)


class OperatorIdentityConfig(BaseModel):
    id: str
    username: str
    display_name: str = ""
    role: Literal["admin", "operator", "viewer"] = "operator"
    api_key_env: Optional[str] = None
    telegram_user_id: Optional[str] = None
    telegram_user_id_env: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("telegram_user_id", mode="before")
    @classmethod
    def coerce_telegram_user_id(cls, val: Any) -> Optional[str]:
        if val is None:
            return None
        cleaned = str(val).strip()
        return cleaned or None


class WebhookIdentityConfig(BaseModel):
    id: str
    name: str
    source_type: str = "alertmanager"
    api_key_env: Optional[str] = None
    scopes: List[str] = Field(default_factory=lambda: ["alerts:ingest"])
    enabled: bool = True


class ExternalClientIdentityConfig(BaseModel):
    id: str
    name: str
    api_key_env: Optional[str] = None
    scopes: List[str] = Field(default_factory=lambda: ["incidents:read", "system:read"])
    enabled: bool = True


class IdentitiesConfig(BaseModel):
    operators: List[OperatorIdentityConfig] = Field(default_factory=list)
    webhooks: List[WebhookIdentityConfig] = Field(default_factory=list)
    external_clients: List[ExternalClientIdentityConfig] = Field(default_factory=list)
    internal_services: List[str] = Field(
        default_factory=lambda: [
            "phase1-worker",
            "phase2-worker",
            "phase3-worker",
            "aegis-telegram-bot",
            "aegis-mcp",
            "aegis-topology-mcp",
            "aegis-api",
            "aegis-cli",
        ]
    )

