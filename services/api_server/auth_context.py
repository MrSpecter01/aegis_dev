"""Helpers for resolving authoritative human/service actor attribution on API requests."""

from typing import Any, Dict, Optional, Tuple
from fastapi import Request

from shared.auth.registry import AuthPrincipal, IdentityRegistry


def _parse_legacy_telegram_actor(raw_actor: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not raw_actor:
        return None, None
    cleaned = str(raw_actor).strip()
    if cleaned.startswith("telegram:"):
        parts = cleaned.split(":", 2)
        uid = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        uname = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        if uname and uname.lower() == "unknown":
            uname = None
        if uid and uid.lower() == "unknown":
            uid = None
        return uid, uname
    return None, None


def resolve_request_actor(
    request: Request,
    req_actor: Optional[str] = None,
    req_channel: str = "dashboard",
    telegram_user_id: Optional[str] = None,
    telegram_username: Optional[str] = None,
    telegram_full_name: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Resolve the authoritative actor string and structured identity metadata for a request.

    - Telegram channel: resolves mapped operator username via `telegram_user_id`, else `@username`,
      else Telegram `full_name`, else `telegram-<id>` (never `unknown`).
    - Operator / external client API keys: binds `principal.username` authoritatively instead of
      trusting hardcoded client strings like `dashboard-operator`.
    - Internal service: allows delegation when explicit non-generic `req_actor` is supplied, else
      attributes to `principal.username`.
    """
    principal: Optional[AuthPrincipal] = getattr(request.state, "principal", None)
    registry: Optional[IdentityRegistry] = getattr(request.app.state, "identity_registry", None)

    raw_actor = (req_actor or "").strip()
    can_delegate_telegram = (
        principal is None
        or principal.entity_type == "internal_service"
        or principal.role == "admin"
    )
    is_telegram_req = (
        can_delegate_telegram
        and (
            req_channel == "telegram"
            or raw_actor.startswith("telegram:")
            or telegram_user_id is not None
        )
    )

    if is_telegram_req:
        parsed_uid, parsed_uname = _parse_legacy_telegram_actor(raw_actor)
        eff_uid = str(telegram_user_id).strip() if telegram_user_id is not None and str(telegram_user_id).strip() else parsed_uid
        eff_uname = (telegram_username or parsed_uname or "").strip() or None
        eff_full = (telegram_full_name or "").strip() or None

        if (
            eff_uid is None
            and eff_uname is None
            and eff_full is None
            and raw_actor
            and raw_actor.lower() not in ("operator", "unknown", "dashboard-operator")
            and not raw_actor.startswith("telegram:")
        ):
            meta: Dict[str, Any] = {"channel": "telegram"}
            if principal:
                meta["key_id"] = principal.key_id
            return raw_actor, meta

        if registry is not None:
            resolved_actor, tg_meta = registry.resolve_telegram_actor(
                eff_uid,
                telegram_username=eff_uname,
                telegram_full_name=eff_full,
            )
        else:
            fallback_name = eff_uname.lstrip("@") if eff_uname else (eff_full or (f"telegram-{eff_uid}" if eff_uid else "telegram-operator"))
            resolved_actor = fallback_name
            tg_meta = {"telegram_user_id": eff_uid, "mapped": False}

        tg_meta["channel"] = "telegram"
        if principal and principal.entity_type == "internal_service":
            tg_meta["via_service"] = principal.username
        elif principal and principal.key_id:
            tg_meta["key_id"] = principal.key_id
        return resolved_actor, tg_meta

    if principal is not None:
        meta = {
            "principal_id": principal.id,
            "key_id": principal.key_id,
            "entity_type": principal.entity_type,
            "role": principal.role,
            "channel": req_channel,
        }
        if principal.entity_type in ("operator", "external_client"):
            if principal.key_id == "legacy-api-key" and raw_actor and raw_actor not in ("dashboard-operator",):
                return raw_actor, meta
            return principal.username, meta
        if principal.entity_type == "internal_service":
            if raw_actor and raw_actor not in ("operator", "dashboard-operator"):
                return raw_actor, meta
            return principal.username, meta
        return principal.username, meta

    return raw_actor or "operator", {"channel": req_channel}

