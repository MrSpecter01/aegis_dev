"""Scoped API key and internal-service authentication middleware."""

import time
from typing import Optional
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from shared.audit.tracker import record_interaction_async
from shared.auth.registry import AuthPrincipal, IdentityRegistry

_POLLING_READ_PREFIXES = (
    "/v1/incidents",
    "/v1/system/status",
    "/v1/audit/interactions",
    "/v1/topology",
)


def _required_scope(method: str, path: str) -> Optional[str]:
    clean_method = method.upper()
    clean_path = path.rstrip("/") or "/"

    if clean_path == "/v1/auth/me":
        return None
    if clean_path == "/v1/alerts/ingest" and clean_method == "POST":
        return "alerts:ingest"
    if clean_path == "/v1/alerts/dropped" and clean_method == "GET":
        return "incidents:read"
    if clean_path.startswith("/v1/incidents"):
        if clean_method == "GET":
            return "incidents:read"
        if clean_path.endswith("/approve") and clean_method == "POST":
            return "approvals:decide"
        if clean_path.endswith("/transition") and clean_method == "POST":
            return "incidents:transition"
        return "incidents:write"
    if clean_path.startswith("/v1/topology"):
        return "topology:read" if clean_method == "GET" else "topology:write"
    if clean_path == "/v1/system/status":
        return "system:read"
    if clean_path in ("/v1/system/pause", "/v1/system/resume"):
        return "system:halt"
    if clean_path == "/v1/system/demo-incident":
        return "incidents:write"
    if clean_path.startswith("/v1/config"):
        return "config:read" if clean_method == "GET" else "config:write"
    if clean_path.startswith("/v1/drift"):
        return "config:read" if clean_method == "GET" else "config:write"
    if clean_path.startswith("/v1/audit"):
        return "audit:read"
    return None


def _should_persist_audit(method: str, path: str, principal: AuthPrincipal, status_code: int) -> bool:
    clean_method = method.upper()
    clean_path = path.rstrip("/") or "/"
    if status_code >= 400:
        return True
    if clean_method != "GET":
        return True
    if clean_path in ("/v1/auth/me",) or clean_path.startswith("/v1/config"):
        return True
    if principal.entity_type in ("external_client", "webhook"):
        return True
    return not clean_path.startswith(_POLLING_READ_PREFIXES)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Authenticates external API keys or internal Docker-only service headers and audits calls."""

    def __init__(
        self,
        app,
        api_key: str = "",
        registry: Optional[IdentityRegistry] = None,
        internal_mode: bool = False,
    ):
        super().__init__(app)
        self.api_key = api_key
        self.registry = registry
        self.internal_mode = internal_mode

    def _get_registry(self, request: Request) -> IdentityRegistry:
        state_reg = getattr(request.app.state, "identity_registry", None)
        if state_reg is not None:
            return state_reg
        if self.registry is not None:
            return self.registry
        return IdentityRegistry.from_config(None, legacy_api_key=self.api_key)

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path in ["/v1/health", "/v1/metrics"]:
            return await call_next(request)

        registry = self._get_registry(request)
        redis_client = getattr(request.app.state, "redis", None)

        header_key = request.headers.get("X-API-Key")
        auth_header = request.headers.get("Authorization", "")
        bearer_key = (
            auth_header[len("Bearer "):].strip()
            if auth_header.startswith("Bearer ")
            else ""
        )
        token = (header_key or bearer_key or "").strip()
        service_header = (request.headers.get("X-Aegis-Service") or "").strip()

        principal: Optional[AuthPrincipal] = None
        if self.internal_mode and service_header:
            principal = registry.authenticate_internal_service(service_header)
            if principal is None:
                await record_interaction_async(
                    redis_client,
                    direction="inbound",
                    category="api_auth",
                    actor=service_header,
                    entity_type="internal_service",
                    target=request.url.path,
                    action=request.method,
                    external=False,
                    status="auth_denied",
                    details={"reason": "unregistered_internal_service"},
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Forbidden: Unregistered internal service '{service_header}'"},
                )
        elif token:
            principal = registry.authenticate_api_key(token)

        if principal is None:
            await record_interaction_async(
                redis_client,
                direction="inbound",
                category="api_auth",
                actor=service_header or "anonymous",
                entity_type="external" if not self.internal_mode else "internal",
                target=request.url.path,
                action=request.method,
                external=not self.internal_mode,
                status="auth_denied",
                details={"reason": "invalid_or_missing_api_key"},
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: Invalid or missing API key"},
            )

        required_scope = _required_scope(request.method, request.url.path)
        if required_scope and not registry.has_scope(principal, required_scope):
            await record_interaction_async(
                redis_client,
                direction="inbound",
                category="api_auth",
                actor=principal.username,
                entity_type=principal.entity_type,
                key_id=principal.key_id,
                target=request.url.path,
                action=request.method,
                external=principal.entity_type != "internal_service",
                status="scope_denied",
                details={"required_scope": required_scope, "granted_scopes": principal.scopes},
            )
            return JSONResponse(
                status_code=403,
                content={"detail": f"Forbidden: Insufficient scope '{required_scope}'"},
            )

        request.state.principal = principal
        t0 = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if _should_persist_audit(request.method, request.url.path, principal, response.status_code):
            await record_interaction_async(
                redis_client,
                direction="inbound",
                category="webhook" if principal.entity_type == "webhook" else "api_request",
                actor=principal.username,
                entity_type=principal.entity_type,
                key_id=principal.key_id,
                target=request.url.path,
                action=request.method,
                external=principal.entity_type != "internal_service",
                status=f"http_{response.status_code}",
                latency_ms=latency_ms,
                details={"role": principal.role, "port_mode": "internal" if self.internal_mode else "external"},
            )

        return response
