import json
import re
from typing import Any, Dict, List, Optional
from shared.config.loader import ConfigManager

INJECTION_PATTERNS = re.compile(
    r"(\b(?:ignore|forget)\s+(?:all\s+|previous\s+|prior\s+|above\s+|system\s+|instructions|rules|prompts)|system:|<|>|;|&&|\|\||`|\$\(|__import__)",
    re.IGNORECASE,
)

FLAG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")
K8S_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
CPU_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?m?$")
MEMORY_PATTERN = re.compile(r"^[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|KB|MB|GB|TB|KiB|MiB|GiB|TiB|m)?$", re.IGNORECASE)


def normalize_k8s_memory_quantity(val: str) -> str:
    """Normalize common memory strings (e.g. 512MB, 512MiB) into standard K8s quantity (512Mi)."""
    cleaned = str(val).strip()
    upper = cleaned.upper()
    m = re.match(r"^([0-9]+)\s*([A-Z]+)$", upper)
    if not m:
        return cleaned
    num, suffix = m.groups()
    if suffix in ("MB", "MIB", "M"):
        return f"{num}Mi"
    if suffix in ("GB", "GIB", "G"):
        return f"{num}Gi"
    if suffix in ("KB", "KIB", "K"):
        return f"{num}Ki"
    if suffix in ("TB", "TIB", "T"):
        return f"{num}Ti"
    return cleaned


def check_injection(params: Dict[str, Any]) -> None:
    """Recursively scan parameter values for injection and escape patterns."""
    def _scan(key: str, val: Any) -> None:
        if isinstance(val, str):
            if INJECTION_PATTERNS.search(val):
                raise ValueError(
                    f"Prompt injection or illegal pattern detected in parameter '{key}': '{val}'"
                )
        elif isinstance(val, dict):
            for sub_k, sub_v in val.items():
                _scan(f"{key}.{sub_k}", sub_v)
        elif isinstance(val, list):
            for idx, item in enumerate(val):
                _scan(f"{key}[{idx}]", item)

    for k, v in params.items():
        _scan(k, v)


def validate_k8s_name(name: str, field_name: str = "name") -> None:
    """Validate Kubernetes DNS-1123 label or subdomain name."""
    if not name or not str(name).strip():
        raise ValueError(f"{field_name} cannot be empty")
    cleaned = str(name).strip()
    if len(cleaned) > 253:
        raise ValueError(f"{field_name} exceeds maximum length of 253 characters")
    if not K8S_NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid {field_name}: '{name}'. Must be a valid Kubernetes DNS label/subdomain."
        )


def validate_replicas(replicas: int, max_replicas: int = 20) -> None:
    """Validate replica count is within allowable safe range."""
    if not isinstance(replicas, int) or isinstance(replicas, bool):
        raise ValueError(f"replicas must be an integer, got {type(replicas).__name__}")
    if replicas < 1:
        raise ValueError(f"replicas must be at least 1, got {replicas}")
    if replicas > max_replicas:
        raise ValueError(
            f"replicas ({replicas}) exceeds maximum configured limit of {max_replicas}"
        )


def validate_grace_period(seconds: int, min_seconds: int = 60, max_seconds: int = 600) -> None:
    """Validate grace period seconds for node draining operations."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ValueError(f"grace_period_seconds must be an integer, got {type(seconds).__name__}")
    if seconds < min_seconds or seconds > max_seconds:
        raise ValueError(
            f"grace_period_seconds must be between {min_seconds} and {max_seconds} seconds, got {seconds}"
        )


def validate_feature_flag_name(flag_name: str) -> None:
    """Validate feature flag identifier charset and length."""
    if not flag_name or not str(flag_name).strip():
        raise ValueError("flag_name cannot be empty")
    cleaned = str(flag_name).strip()
    if not FLAG_NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid flag_name: '{flag_name}'. Must match pattern '^[a-zA-Z0-9_\\-\\.]+$' (1-128 chars)."
        )


def validate_environment(environment: str, allowed_environments: Optional[List[str]] = None) -> None:
    """Validate environment against configured whitelist."""
    if not environment or not str(environment).strip():
        raise ValueError("environment cannot be empty")
    cleaned = str(environment).strip()
    allowed = allowed_environments
    if allowed is None:
        try:
            cfg = ConfigManager().sag
            flag_policy = cfg.get_tool_policy("toggle_feature_flag")
            allowed = flag_policy.allowed_environments or ["production", "staging", "development"]
        except Exception:
            allowed = ["production", "staging", "development"]

    if cleaned not in allowed:
        raise ValueError(
            f"Environment '{environment}' is not permitted. Allowed environments: {allowed}"
        )


def parse_cpu_to_millicores(val: str) -> float:
    """Parse k8s cpu quantity string to millicores."""
    val = val.strip()
    if val.endswith("m"):
        return float(val[:-1])
    return float(val) * 1000.0


def parse_memory_to_bytes(val: str) -> int:
    """Parse k8s memory quantity string to bytes."""
    val = val.strip()
    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "Pi": 1024 ** 5,
        "Ei": 1024 ** 6,
        "k": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
        "P": 1000 ** 5,
        "E": 1000 ** 6,
    }
    for unit, mult in units.items():
        if val.endswith(unit):
            return int(float(val[:-len(unit)]) * mult)
    return int(val)


def validate_resource_limits(
    limits: Dict[str, Any],
    current_limits: Optional[Dict[str, Any]] = None,
    max_cpu_ratio: float = 2.0,
    max_mem_ratio: float = 2.0,
) -> None:
    """Validate container resource limit dictionary structure, syntax, and ratio bounds."""
    if not isinstance(limits, dict) or not limits:
        raise ValueError("limits must be a non-empty dictionary containing 'cpu' and/or 'memory'")

    allowed_keys = {"cpu", "memory"}
    unknown = set(limits.keys()) - allowed_keys
    if unknown:
        raise ValueError(f"Invalid resource limits keys: {unknown}. Only 'cpu' and 'memory' are supported.")

    if "cpu" in limits:
        cpu_val = str(limits["cpu"]).strip()
        if not CPU_PATTERN.match(cpu_val):
            raise ValueError(
                f"Invalid cpu format: '{cpu_val}'. Expected millicores (e.g. '300m') or cores (e.g. '1', '0.5')."
            )
        req_cpu = parse_cpu_to_millicores(cpu_val)
        if current_limits and "cpu" in current_limits:
            curr_cpu_val = str(current_limits["cpu"]).strip()
            if CPU_PATTERN.match(curr_cpu_val):
                curr_cpu = parse_cpu_to_millicores(curr_cpu_val)
                if curr_cpu > 0 and req_cpu > curr_cpu * max_cpu_ratio:
                    raise ValueError(
                        f"Requested cpu '{cpu_val}' ({req_cpu:.0f}m) exceeds maximum allowed increase ratio of "
                        f"{max_cpu_ratio}x over current limit '{curr_cpu_val}' ({curr_cpu:.0f}m)."
                    )

    if "memory" in limits:
        mem_val = str(limits["memory"]).strip()
        norm_mem = normalize_k8s_memory_quantity(mem_val)
        if not MEMORY_PATTERN.match(mem_val) and not MEMORY_PATTERN.match(norm_mem):
            raise ValueError(
                f"Invalid memory format: '{mem_val}'. Expected quantity with suffix (e.g. '128Mi', '1Gi')."
            )
        limits["memory"] = norm_mem
        mem_val = norm_mem
        req_mem = parse_memory_to_bytes(mem_val)
        if current_limits and "memory" in current_limits:
            curr_mem_val = str(current_limits["memory"]).strip()
            if MEMORY_PATTERN.match(curr_mem_val):
                curr_mem = parse_memory_to_bytes(curr_mem_val)
                if curr_mem > 0 and req_mem > curr_mem * max_mem_ratio:
                    raise ValueError(
                        f"Requested memory '{mem_val}' ({req_mem} bytes) exceeds maximum allowed increase ratio of "
                        f"{max_mem_ratio}x over current limit '{curr_mem_val}' ({curr_mem} bytes)."
                    )


def is_tool_allowed(tool_name: str, allowed_tools: Any, sag_config: Optional[Any] = None) -> bool:
    """Dynamically determine if tool_name matches any tool in allowed_tools.

    Resolves:
    1. Exact match (tool_name in allowed_tools)
    2. Namespace suffix/prefix match (e.g. k8s_restart_pod matches restart_pod)
    3. Configured aliases dynamically loaded from sag_policies.yaml
    """
    if not allowed_tools:
        return True

    allowed_set = set(allowed_tools) if isinstance(allowed_tools, (set, list, tuple)) else {allowed_tools}
    if tool_name in allowed_set:
        return True

    # 1. Namespace prefix/suffix matches
    for allowed in allowed_set:
        if tool_name.endswith(f"_{allowed}") or allowed.endswith(f"_{tool_name}"):
            return True

    # 2. Configured tool aliases dynamically from SAG policies
    cfg = sag_config
    if cfg is None:
        try:
            cfg = ConfigManager().sag
        except Exception:
            cfg = None

    if cfg and hasattr(cfg, "tools"):
        for canonical, policy in cfg.tools.items():
            aliases = getattr(policy, "aliases", []) or []
            family = {canonical, *aliases}
            tool_in_family = any(
                tool_name == member or tool_name.endswith(f"_{member}") or member.endswith(f"_{tool_name}")
                for member in family
            )
            if tool_in_family:
                for allowed in allowed_set:
                    if any(
                        allowed == member or allowed.endswith(f"_{member}") or member.endswith(f"_{allowed}")
                        for member in family
                    ):
                        return True

    return False


async def verify_action_approved(
    redis_client: Any,
    incident_id: str,
    action_id: str,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Authoritative Redis approval check for Safety Action Gateway.
    Raises PermissionError if the action is not found, expired, or not in 'approved' status,
    or if tool_name is provided and not part of the approved action proposal.
    """
    if redis_client is None:
        raise PermissionError("Redis client is not available to verify action approval")

    key = f"incident:{incident_id}:action:{action_id}"
    raw = await redis_client.get(key)

    if not raw:
        raise PermissionError(
            f"Action '{action_id}' not found in approval store for incident '{incident_id}'"
        )

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")

    if isinstance(raw, str):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PermissionError(f"Corrupted action record in Redis for '{action_id}': {exc}")
    elif isinstance(raw, dict):
        record = raw
    else:
        raise PermissionError(f"Unexpected record type {type(raw)} in Redis for action '{action_id}'")

    status = record.get("status")
    if status == "denied":
        actor = record.get("actor", "system")
        channel = record.get("channel", "unknown")
        raise PermissionError(
            f"Action '{action_id}' was explicitly DENIED by {actor} via {channel}. Execution blocked permanently."
        )

    if status != "approved":
        raise PermissionError(
            f"Action '{action_id}' is not approved (current status: '{status}'). Approval required before execution."
        )

    if tool_name is not None:
        actions_list = record.get("proposal", {}).get("actions", [])
        if not actions_list and "actions" in record:
            actions_list = record.get("actions", [])
        allowed = {a.get("tool_name") for a in actions_list if isinstance(a, dict)}
        if record.get("tool_name"):
            allowed.add(record.get("tool_name"))
        if allowed:
            if not is_tool_allowed(tool_name, allowed):
                raise PermissionError(f"Tool '{tool_name}' is not part of approved action '{action_id}' (allowed: {allowed})")

    return record


async def validate_incident_scope(
    redis_client: Any,
    incident_id: str,
    namespace: Optional[str] = None,
    target_resource: Optional[str] = None,
    tool_name: Optional[str] = None,
    sag_config: Optional[Any] = None,
) -> None:
    """
    Authoritative Scope and Namespace validation for Safety Action Gateway.

    Enforces two-tier validation:
    1. Global / Tool Policy Namespace Whitelist (Tier 1):
       Verifies `namespace` is authorized by SAG policy.
    2. Incident Context Scope (Tier 2):
       Verifies `namespace` matches the incident's affected_namespace (or related alerts).
       If `target_resource` is provided, verifies it matches the affected_service.
    """
    # 1. Tier 1: Global / Tool Policy Namespace Whitelist
    cfg = sag_config
    if cfg is None:
        try:
            cfg = ConfigManager().sag
        except Exception:
            cfg = None

    if cfg and namespace:
        policy = cfg.get_tool_policy(tool_name) if tool_name and hasattr(cfg, "get_tool_policy") else None
        tool_allowed_ns = getattr(policy, "allowed_namespaces", []) or []
        global_allowed_ns = getattr(cfg, "allowed_namespaces", []) or []

        allowed_ns_list = tool_allowed_ns or global_allowed_ns
        if allowed_ns_list and namespace not in allowed_ns_list:
            raise PermissionError(
                f"Namespace '{namespace}' is not permitted by SAG policy. Authorized namespaces: {allowed_ns_list}"
            )

    # 2. Tier 2: Incident Context Boundary Check from Redis
    if redis_client is not None and incident_id:
        raw_state = await redis_client.get(f"incident:{incident_id}:state")
        if raw_state:
            if isinstance(raw_state, (bytes, bytearray)):
                raw_state = raw_state.decode("utf-8")
            state_data = json.loads(raw_state) if isinstance(raw_state, str) else raw_state
            expected_ns = state_data.get("affected_namespace")
            expected_svc = state_data.get("affected_service")

            allowed_incident_ns = {expected_ns} if expected_ns else set()

            # Include related alert namespaces if cross-namespace incident
            raw_grp = await redis_client.get(f"incident:{incident_id}:alert_group")
            if raw_grp:
                if isinstance(raw_grp, (bytes, bytearray)):
                    raw_grp = raw_grp.decode("utf-8")
                grp_data = json.loads(raw_grp) if isinstance(raw_grp, str) else raw_grp
                for a in grp_data.get("related_alerts", []):
                    ns_lbl = a.get("labels", {}).get("namespace") if isinstance(a, dict) else getattr(a, "namespace", None)
                    if ns_lbl:
                        allowed_incident_ns.add(ns_lbl)

            if namespace and allowed_incident_ns and namespace not in allowed_incident_ns:
                raise PermissionError(
                    f"Action rejected by SAG: Proposed namespace '{namespace}' does not match incident authorized scope {sorted(list(allowed_incident_ns))}."
                )

            if target_resource and expected_svc:
                res_name = target_resource.split("/")[-1]
                if not (res_name == expected_svc or res_name.startswith(f"{expected_svc}-") or expected_svc in res_name):
                    raise PermissionError(
                        f"Action rejected by SAG: Target resource '{target_resource}' is outside the incident affected service boundary '{expected_svc}'."
                    )


_redis_client: Optional[Any] = None


def set_redis_client(client: Optional[Any]) -> None:
    """Set global Redis client (useful for unit testing mocks)."""
    global _redis_client
    _redis_client = client


async def get_redis_client() -> Any:
    """Resolve active Redis client instance."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    import redis.asyncio as aioredis
    config = ConfigManager()
    url = config.system.redis.url

    _redis_client = aioredis.from_url(url, decode_responses=True)
    return _redis_client

