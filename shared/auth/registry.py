"""Identity registry, scoped API key validation, and Telegram-operator resolution."""

import base64
import hmac
import logging
import os
from typing import Any, Dict, List, Literal, Optional, Set, Tuple
from pydantic import BaseModel, Field

from shared.config.schemas import IdentitiesConfig, OperatorIdentityConfig

logger = logging.getLogger("aegis.auth")

ROLE_DEFAULT_SCOPES: Dict[str, List[str]] = {
    "admin": ["*"],
    "operator": [
        "incidents:read",
        "incidents:write",
        "approvals:decide",
        "system:read",
        "system:halt",
        "topology:read",
        "config:read",
        "audit:read",
    ],
    "viewer": [
        "incidents:read",
        "system:read",
        "topology:read",
        "config:read",
        "audit:read",
    ],
}


def build_auth_headers(
    api_key: Optional[str],
    auth_type: str = "bearer",
    auth_header: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build HTTP authentication headers from an outbound endpoint configuration."""
    merged: Dict[str, str] = dict(extra_headers) if extra_headers else {}
    if api_key and auth_type != "none":
        if auth_type == "bearer":
            header_name = auth_header or "Authorization"
            merged[header_name] = f"Bearer {api_key}"
        elif auth_type in ("api_key", "header"):
            header_name = auth_header or "X-API-Key"
            merged[header_name] = api_key
        elif auth_type == "basic":
            header_name = auth_header or "Authorization"
            encoded = base64.b64encode(api_key.encode("utf-8")).decode("ascii")
            merged[header_name] = f"Basic {encoded}"
    return merged


def get_endpoint_auth_headers(domain: str, mcp_endpoints: Any = None) -> Dict[str, str]:
    """Resolve outbound HTTP auth headers for a built-in mcp_endpoints domain (prometheus, loki, tempo, jaeger, flagd)."""
    if mcp_endpoints is None:
        from shared.config.loader import ConfigManager
        mcp_endpoints = ConfigManager().system.mcp_endpoints
    api_key = getattr(mcp_endpoints, f"{domain}_api_key", "") or ""
    if not api_key:
        return {}
    auth_type = getattr(mcp_endpoints, f"{domain}_auth_type", "bearer") or "bearer"
    auth_header = getattr(mcp_endpoints, f"{domain}_auth_header", "") or None
    return build_auth_headers(api_key=api_key, auth_type=auth_type, auth_header=auth_header)


class AuthPrincipal(BaseModel):
    """Authenticated caller identity attached to API requests and audit events."""

    id: str
    username: str
    display_name: str = ""
    entity_type: Literal["operator", "webhook", "external_client", "internal_service"]
    role: str = "operator"
    scopes: List[str] = Field(default_factory=list)
    key_id: Optional[str] = None
    telegram_user_id: Optional[str] = None
    channel: Optional[str] = None


class IdentityRegistry:
    """Loads multi-entity identities and authenticates inbound keys and Telegram actors."""

    def __init__(
        self,
        key_records: Optional[List[Tuple[str, AuthPrincipal]]] = None,
        telegram_operators: Optional[Dict[str, OperatorIdentityConfig]] = None,
        internal_services: Optional[Set[str]] = None,
        identities_cfg: Optional[IdentitiesConfig] = None,
        configured_ids: Optional[Set[str]] = None,
        telegram_configured_ids: Optional[Set[str]] = None,
    ) -> None:
        self._key_records: List[Tuple[str, AuthPrincipal]] = key_records or []
        self._telegram_operators: Dict[str, OperatorIdentityConfig] = telegram_operators or {}
        self.internal_services: Set[str] = internal_services or {
            "phase1-worker",
            "phase2-worker",
            "phase3-worker",
            "aegis-telegram-bot",
            "aegis-mcp",
            "aegis-topology-mcp",
            "aegis-api",
            "aegis-cli",
        }
        self._identities_cfg = identities_cfg or IdentitiesConfig()
        self._configured_ids: Set[str] = configured_ids or set()
        self._telegram_configured_ids: Set[str] = telegram_configured_ids or set()

    @classmethod
    def from_config(
        cls,
        identities_cfg: Any = None,
        legacy_api_key: Optional[str] = None,
    ) -> "IdentityRegistry":
        """Build an IdentityRegistry from IdentitiesConfig plus optional legacy AEGIS_API_KEY."""
        cfg = identities_cfg if isinstance(identities_cfg, IdentitiesConfig) else IdentitiesConfig()
        key_records: List[Tuple[str, AuthPrincipal]] = []
        telegram_operators: Dict[str, OperatorIdentityConfig] = {}
        configured_ids: Set[str] = set()
        telegram_configured_ids: Set[str] = set()

        def _register_secret(secret: str, principal: AuthPrincipal) -> None:
            for existing_secret, existing_principal in key_records:
                if hmac.compare_digest(existing_secret, secret):
                    logger.warning(
                        "API key collision between identity '%s' and '%s'; keeping first binding",
                        existing_principal.id,
                        principal.id,
                    )
                    return
            key_records.append((secret, principal))
            configured_ids.add(principal.id)

        def _read_env_secret(env_name: Optional[str]) -> str:
            if not env_name or not env_name.strip():
                return ""
            val = os.getenv(env_name.strip())
            return val.strip() if val else ""

        for op in cfg.operators:
            if not op.enabled:
                continue
            resolved_tg_id = _read_env_secret(op.telegram_user_id_env) or (
                str(op.telegram_user_id).strip() if op.telegram_user_id else ""
            )
            if resolved_tg_id:
                telegram_operators[resolved_tg_id] = op
                telegram_configured_ids.add(op.id)
            secret = _read_env_secret(op.api_key_env)
            if secret:
                scopes = list(op.scopes) if op.scopes else list(ROLE_DEFAULT_SCOPES.get(op.role, ROLE_DEFAULT_SCOPES["operator"]))
                principal = AuthPrincipal(
                    id=op.id,
                    username=op.username,
                    display_name=op.display_name or op.username,
                    entity_type="operator",
                    role=op.role,
                    scopes=scopes,
                    key_id=f"env:{op.api_key_env}",
                    telegram_user_id=resolved_tg_id or None,
                )
                _register_secret(secret, principal)

        for wh in cfg.webhooks:
            if not wh.enabled:
                continue
            secret = _read_env_secret(wh.api_key_env)
            if secret:
                scopes = list(wh.scopes) if wh.scopes else ["alerts:ingest"]
                principal = AuthPrincipal(
                    id=wh.id,
                    username=wh.name,
                    display_name=f"Webhook ({wh.source_type}: {wh.name})",
                    entity_type="webhook",
                    role="webhook",
                    scopes=scopes,
                    key_id=f"env:{wh.api_key_env}",
                )
                _register_secret(secret, principal)

        for ext in cfg.external_clients:
            if not ext.enabled:
                continue
            secret = _read_env_secret(ext.api_key_env)
            if secret:
                scopes = list(ext.scopes) if ext.scopes else ["incidents:read", "system:read"]
                principal = AuthPrincipal(
                    id=ext.id,
                    username=ext.name,
                    display_name=ext.name,
                    entity_type="external_client",
                    role="external_client",
                    scopes=scopes,
                    key_id=f"env:{ext.api_key_env}",
                )
                _register_secret(secret, principal)

        clean_legacy = str(legacy_api_key or "").strip()
        if clean_legacy:
            already_registered = any(hmac.compare_digest(s, clean_legacy) for s, _ in key_records)
            if not already_registered:
                _register_secret(
                    clean_legacy,
                    AuthPrincipal(
                        id="op-legacy-admin",
                        username="admin",
                        display_name="Aegis Admin",
                        entity_type="operator",
                        role="admin",
                        scopes=["*"],
                        key_id="legacy-api-key",
                    ),
                )

        internal_services = {str(s).strip() for s in (cfg.internal_services or []) if str(s).strip()}
        return cls(
            key_records=key_records,
            telegram_operators=telegram_operators,
            internal_services=internal_services or None,
            identities_cfg=cfg,
            configured_ids=configured_ids,
            telegram_configured_ids=telegram_configured_ids,
        )

    def authenticate_api_key(self, token: Optional[str]) -> Optional[AuthPrincipal]:
        """Validate an API key against registered external identities using constant-time comparison."""
        cleaned = str(token or "").strip()
        if not cleaned:
            return None
        for secret, principal in self._key_records:
            if hmac.compare_digest(cleaned, secret):
                return principal
        return None

    def authenticate_internal_service(self, service_name: Optional[str]) -> Optional[AuthPrincipal]:
        """Validate a known internal Aegis service identity on the internal-only API port."""
        cleaned = str(service_name or "").strip()
        if not cleaned or cleaned not in self.internal_services:
            return None
        return AuthPrincipal(
            id=f"svc-{cleaned}",
            username=cleaned,
            display_name=cleaned,
            entity_type="internal_service",
            role="internal_service",
            scopes=["*"],
            key_id=f"internal-service:{cleaned}",
            channel="internal",
        )

    @staticmethod
    def has_scope(principal: Optional[AuthPrincipal], required_scope: Optional[str]) -> bool:
        """Check whether an authenticated principal satisfies the required permission scope."""
        if principal is None:
            return False
        if not required_scope:
            return True
        scopes = principal.scopes or []
        if "*" in scopes or required_scope in scopes:
            return True
        for s in scopes:
            if s.endswith(":*"):
                prefix = s[:-1]
                if required_scope.startswith(prefix):
                    return True
        return False

    def resolve_telegram_actor(
        self,
        telegram_user_id: Optional[str],
        telegram_username: Optional[str] = None,
        telegram_full_name: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Resolve a Telegram sender to an Aegis operator username, handle, full name, or user-ID label."""
        tid = str(telegram_user_id).strip() if telegram_user_id is not None else ""
        if tid.lower() == "unknown":
            tid = ""

        t_user = str(telegram_username).strip().lstrip("@") if telegram_username else ""
        if t_user.lower() == "unknown":
            t_user = ""

        t_full = str(telegram_full_name).strip() if telegram_full_name else ""
        if t_full.lower() == "unknown":
            t_full = ""

        if tid and tid in self._telegram_operators:
            op = self._telegram_operators[tid]
            meta: Dict[str, Any] = {
                "operator_id": op.id,
                "display_name": op.display_name or op.username,
                "telegram_user_id": tid,
                "mapped": True,
            }
            if t_user:
                meta["telegram_username"] = t_user
            if t_full:
                meta["telegram_full_name"] = t_full
            return op.username, meta

        if t_user:
            meta = {"telegram_username": t_user, "mapped": False}
            if tid:
                meta["telegram_user_id"] = tid
            if t_full:
                meta["telegram_full_name"] = t_full
            return t_user, meta

        if t_full:
            meta = {"telegram_full_name": t_full, "mapped": False}
            if tid:
                meta["telegram_user_id"] = tid
            return t_full, meta

        fallback = f"telegram-{tid}" if tid else "telegram-operator"
        meta = {"mapped": False}
        if tid:
            meta["telegram_user_id"] = tid
        return fallback, meta

    def list_public_identities(self) -> Dict[str, Any]:
        """Return a redacted summary of configured identities (never exposes secret values)."""
        return {
            "operators": [
                {
                    "id": op.id,
                    "username": op.username,
                    "display_name": op.display_name or op.username,
                    "role": op.role,
                    "scopes": op.scopes or ROLE_DEFAULT_SCOPES.get(op.role, ROLE_DEFAULT_SCOPES["operator"]),
                    "telegram_user_id": op.telegram_user_id,
                    "telegram_user_id_env": op.telegram_user_id_env,
                    "telegram_configured": op.id in self._telegram_configured_ids,
                    "api_key_env": op.api_key_env,
                    "key_configured": op.id in self._configured_ids,
                    "enabled": op.enabled,
                }
                for op in self._identities_cfg.operators
            ],
            "webhooks": [
                {
                    "id": wh.id,
                    "name": wh.name,
                    "source_type": wh.source_type,
                    "scopes": wh.scopes,
                    "api_key_env": wh.api_key_env,
                    "key_configured": wh.id in self._configured_ids,
                    "enabled": wh.enabled,
                }
                for wh in self._identities_cfg.webhooks
            ],
            "external_clients": [
                {
                    "id": ext.id,
                    "name": ext.name,
                    "scopes": ext.scopes,
                    "api_key_env": ext.api_key_env,
                    "key_configured": ext.id in self._configured_ids,
                    "enabled": ext.enabled,
                }
                for ext in self._identities_cfg.external_clients
            ],
            "internal_services": sorted(self.internal_services),
        }
