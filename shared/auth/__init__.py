from shared.auth.registry import (
    AuthPrincipal,
    IdentityRegistry,
    ROLE_DEFAULT_SCOPES,
    build_auth_headers,
    get_endpoint_auth_headers,
)

__all__ = [
    "AuthPrincipal",
    "IdentityRegistry",
    "ROLE_DEFAULT_SCOPES",
    "build_auth_headers",
    "get_endpoint_auth_headers",
]
