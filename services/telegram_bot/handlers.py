import functools
import logging
from typing import Any, Dict, Optional, Tuple
import httpx
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger("aegis.telegram_bot.handlers")


def authorized_only(func):
    """Decorator ensuring incoming update originates from authorized chat_id."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        allowed = {str(context.bot_data.get("chat_id", "")).strip()} - {""}
        chat = update.effective_chat
        ok = chat is not None and str(chat.id) in allowed
        if not ok:
            logger.warning(
                "Unauthorized Telegram access chat=%s user=%s",
                getattr(chat, "id", None),
                getattr(update.effective_user, "id", None),
            )
            if update.callback_query:
                try:
                    await update.callback_query.answer("Not authorised", show_alert=True)
                except Exception as exc:
                    logger.warning("Failed to answer unauthorized callback query: %s", exc)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def _get_api_client(context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, Dict[str, str]]:
    api_url = context.bot_data.get("aegis_api_url", "").rstrip("/")
    api_key = context.bot_data.get("aegis_api_key", "")
    headers: Dict[str, str] = {"X-Aegis-Service": "aegis-telegram-bot"}
    if api_key:
        headers["X-API-Key"] = api_key
    return api_url, headers


def _extract_telegram_user(user: Any) -> Tuple[str, Dict[str, Any]]:
    """Extract clean sender identity fields from a Telegram User object."""
    raw_id = getattr(user, "id", None) if user is not None else None
    telegram_user_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None

    raw_username = getattr(user, "username", None) if user is not None else None
    telegram_username = str(raw_username).strip().lstrip("@") if raw_username else None
    if telegram_username and telegram_username.lower() == "unknown":
        telegram_username = None

    raw_full = getattr(user, "full_name", None) if user is not None else None
    if not raw_full and user is not None:
        first = str(getattr(user, "first_name", "") or "").strip()
        last = str(getattr(user, "last_name", "") or "").strip()
        raw_full = f"{first} {last}".strip()
    telegram_full_name = str(raw_full).strip() if raw_full else None
    if telegram_full_name and telegram_full_name.lower() == "unknown":
        telegram_full_name = None

    fallback_actor = (
        telegram_username
        or telegram_full_name
        or (f"telegram-{telegram_user_id}" if telegram_user_id else "telegram-operator")
    )
    fields: Dict[str, Any] = {}
    if telegram_user_id:
        fields["telegram_user_id"] = telegram_user_id
    if telegram_username:
        fields["telegram_username"] = telegram_username
    if telegram_full_name:
        fields["telegram_full_name"] = telegram_full_name
    return fallback_actor, fields


def _extract_resolved_actor(resp: Any, fallback_actor: str) -> str:
    """Read server-resolved actor name from JSON response when available."""
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("actor"):
            return str(data["actor"])
    except Exception:
        pass
    return fallback_actor


async def safe_reply(message, text: str, **kwargs):
    """Send a reply with Markdown, falling back to plain text if entity parsing fails."""
    if not message:
        return None
    try:
        return await message.reply_text(text, parse_mode="Markdown", **kwargs)
    except Exception as exc:
        if "can't parse entities" in str(exc).lower():
            logger.warning("Markdown parse failed (%s); sending plain text fallback", exc)
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
            return await message.reply_text(text, parse_mode=None, **clean_kwargs)
        raise


@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to /start with real-time Aegis system status summary."""
    if not update.effective_message:
        return

    api_url, headers = _get_api_client(context)

    health_status = "Offline"
    active_incidents = 0
    breakdown = {}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            h_resp = await client.get(f"{api_url}/v1/health", headers=headers)
            if h_resp.status_code == 200:
                health_data = h_resp.json()
                redis_st = health_data.get("redis", "unknown")
                health_status = f"Online (redis: {redis_st})"

            i_resp = await client.get(f"{api_url}/v1/incidents", headers=headers)
            if i_resp.status_code == 200:
                incidents = i_resp.json()
                active_incidents = len(incidents)
                for inc in incidents:
                    st = inc.get("state", "UNKNOWN")
                    breakdown[st] = breakdown.get(st, 0) + 1
    except Exception as exc:
        health_status = f"Degraded ({exc})"

    breakdown_str = ", ".join(f"{k}: {v}" for k, v in breakdown.items()) if breakdown else "none"

    text = (
        f"🛡️ *Aegis Incident Response Bot*\n\n"
        f"• *Control Plane:* {health_status}\n"
        f"• *Active Incidents:* `{active_incidents}` (`{breakdown_str}`)\n"
        f"• *Bot Service:* Ready & listening for incident alerts\n\n"
        f"📋 *Available Commands:*\n"
        f"• `/status` — System health & metrics\n"
        f"• `/incidents` — List open incidents\n"
        f"• `/incident <id>` — View incident details\n"
        f"• `/approve <id> [reason]` — Authorize pending remediation action\n"
        f"• `/deny <id> [reason]` — Deny pending action & escalate\n"
        f"• `/steer <id> <guidance>` — Provide steering instructions\n"
        f"• `/escalate <id> <reason>` — Manually escalate incident\n"
        f"• `/close <id> <reason>` — Force-close incident\n"
        f"• `/approve_drift <topology|runbooks>` — Approve pending SHA-256 drift sync\n"
        f"• `/reject_drift <topology|runbooks> [reason]` — Dismiss pending drift\n"
        f"• `/help` — Command reference"
    )

    await safe_reply(update.effective_message, text)


@authorized_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to /status with system health."""
    await start_command(update, context)


@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display command usage help."""
    if not update.effective_message:
        return

    text = (
        f"📖 *Aegis Bot Command Reference*\n\n"
        f"• `/start` or `/status` — System health and incident counts\n"
        f"• `/incidents` — View all currently open incidents\n"
        f"• `/incident <id>` — Look up details of a specific incident\n"
        f"• `/approve <id> [reason]` — Authorize pending remediation action for an incident\n"
        f"• `/deny <id> [reason]` — Deny pending remediation action for an incident\n"
        f"• `/drop <id> [reason]` — Drop issue & halt all agent operations on incident\n"
        f"• `/steer <id> <instruction>` — Send steering guidance to re-plan remediation or resume escalated incidents\n"
        f"• `/escalate <id> <reason>` — Force an incident to `ESCALATED`\n"
        f"• `/close <id> <reason>` — Force-close an incident\n"
        f"• `/approve_drift <topology|runbooks>` — Approve pending SHA-256 drift sync\n"
        f"• `/reject_drift <topology|runbooks> [reason]` — Dismiss pending drift alert\n\n"
        f"When an action requires approval, click the inline buttons:\n"
        f"• `[✅ Approve]` — Authorize execution through the Safety Action Gateway\n"
        f"• `[❌ Deny]` — Deny action and escalate\n"
        f"• `[🔄 Steer Plan]` — Prompt to redirect the remediation plan"
    )
    await safe_reply(update.effective_message, text)


@authorized_only
async def incidents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all open incidents."""
    if not update.effective_message:
        return

    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{api_url}/v1/incidents", headers=headers)
            if resp.status_code != 200:
                await update.effective_message.reply_text(
                    f"❌ Failed to fetch incidents: HTTP {resp.status_code}"
                )
                return
            incidents = resp.json()
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Connection error: {exc}")
        return

    if not incidents:
        await update.effective_message.reply_text("✅ No active incidents. All systems nominal.")
        return

    active = [i for i in incidents if i.get("state") not in ("CLOSED", "RESOLVED")]
    closed = [i for i in incidents if i.get("state") in ("CLOSED", "RESOLVED")]

    lines = []
    if active:
        lines.append(f"📋 *Active Aegis Incidents ({len(active)}):*\n")
        for inc in active:
            inc_id = inc.get("incident_id", "Unknown")
            sev = inc.get("severity", "P3").upper()
            state = inc.get("state", "UNKNOWN")
            svc = inc.get("affected_service", "Unknown")
            emoji = "🚨" if sev == "P1" else ("⚠️" if sev == "P2" else "ℹ️")
            lines.append(f"{emoji} `{inc_id}` *[{sev}]* — `{state}` on `{svc}`")
        lines.append("")

    if closed:
        lines.append(f"📁 *Recent Closed Incidents ({len(closed)}):*\n")
        for inc in closed[:5]:
            inc_id = inc.get("incident_id", "Unknown")
            sev = inc.get("severity", "P3").upper()
            svc = inc.get("affected_service", "Unknown")
            lines.append(f"✅ `{inc_id}` [{sev}] on `{svc}`")

    if not lines:
        lines = ["✅ No active incidents. All systems nominal."]

    await safe_reply(update.effective_message, "\n".join(lines).strip())


@authorized_only
async def incident_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Look up details of a single incident."""
    if not update.effective_message:
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: `/incident <incident_id>`", parse_mode="Markdown")
        return

    incident_id = args[0].strip()
    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{api_url}/v1/incidents/{incident_id}", headers=headers)
            if resp.status_code == 404:
                await update.effective_message.reply_text(f"❌ Incident `{incident_id}` not found.")
                return
            elif resp.status_code != 200:
                await update.effective_message.reply_text(f"❌ API error: {resp.status_code}")
                return
            inc = resp.json()
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Error: {exc}")
        return

    action_obj = inc.get("action") or {}
    proposal_obj = action_obj.get("proposal") or {}
    citations = (
        proposal_obj.get("runbook_citations")
        or action_obj.get("runbook_citations")
        or []
    )
    cit_block = ""
    if isinstance(citations, list) and citations:
        c_lines = ["\n📚 *Cited Runbooks:*"]
        for c in citations[:3]:
            if isinstance(c, dict):
                b = str(c.get("book_name") or c.get("runbook_id") or "Runbook").strip()
                r_id = str(c.get("runbook_id") or "").strip()
                id_title = f"{r_id}/{b}" if (r_id and b and r_id.lower() != b.lower()) else (r_id or b)
                sec = str(c.get("section") or "General").strip()
                l_rng = str(c.get("line_range") or "").strip()
                c_id = str(c.get("chunk_id") or "").strip()
                sec_loc = f"{sec}/{l_rng}" if l_rng else sec
                if c_id:
                    sec_loc += f" ({c_id})"
                c_lines.append(f"• `id/title:` '{id_title}'\n  `section/line_range:` '{sec_loc}'")
        cit_block = "\n".join(c_lines)

    text = (
        f"🔍 *Incident Details:* `{incident_id}`\n\n"
        f"• *State:* `{inc.get('state', 'UNKNOWN')}`\n"
        f"• *Severity:* *{inc.get('severity', 'P3').upper()}*\n"
        f"• *Affected Service:* `{inc.get('affected_service', 'Unknown')}`\n"
        f"• *Namespace:* `{inc.get('affected_namespace', 'default')}`\n"
        f"• *Retry Count:* `{inc.get('retry_count', 0)}`\n"
        f"• *Updated:* `{inc.get('updated_at', '')}`"
        f"{cit_block}"
    )
    await safe_reply(update.effective_message, text)


@authorized_only
async def steer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send operator steering instructions to resume/redirect remediation."""
    if not update.effective_message:
        return

    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: `/steer <incident_id> <instruction text>`", parse_mode="Markdown"
        )
        return

    incident_id = args[0].strip()
    instruction = " ".join(args[1:]).strip()
    actor, tg_fields = _extract_telegram_user(update.effective_user)

    api_url, headers = _get_api_client(context)

    payload = {
        "instruction": instruction,
        "actor": actor,
        "channel": "telegram",
        **tg_fields,
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(f"{api_url}/v1/incidents/{incident_id}/steer", json=payload, headers=headers)
            if resp.status_code == 200:
                resolved_actor = _extract_resolved_actor(resp, actor)
                await safe_reply(
                    update.effective_message,
                    f"🔄 *Steering Guidance Applied*\n\n"
                    f"• *Incident:* `{incident_id}`\n"
                    f"• *Instruction:* `{instruction}`\n"
                    f"• *Operator:* `{resolved_actor}`\n\n"
                    f"Incident transitioned to `REMEDIATION`. Agent will re-plan using guidance.",
                )
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await update.effective_message.reply_text(f"❌ Steer failed ({resp.status_code}): {detail}")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Error communicating with Aegis API: {exc}")


@authorized_only
async def escalate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-escalate an active incident."""
    if not update.effective_message:
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/escalate <incident_id> [reason]`", parse_mode="Markdown"
        )
        return

    incident_id = args[0].strip()
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Manual escalation via Telegram"
    actor, tg_fields = _extract_telegram_user(update.effective_user)

    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{api_url}/v1/incidents/{incident_id}/escalate",
                json={"reason": reason, "actor": actor, "channel": "telegram", **tg_fields},
                headers=headers,
            )
            if resp.status_code == 200:
                await safe_reply(
                    update.effective_message,
                    f"⚠️ Incident `{incident_id}` escalated successfully.\nReason: `{reason}`",
                )
            else:
                await update.effective_message.reply_text(f"❌ Escalation failed: HTTP {resp.status_code}")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Error: {exc}")


@authorized_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a pending remediation action for an incident."""
    if not update.effective_message:
        return

    args = context.args or []
    if not args:
        await safe_reply(update.effective_message, "Usage: `/approve <incident_id> [reason]`")
        return

    incident_id = args[0].strip()
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Manual approval via Telegram"
    actor, tg_fields = _extract_telegram_user(update.effective_user)

    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            act_resp = await client.get(f"{api_url}/v1/incidents/{incident_id}/action", headers=headers)
            if act_resp.status_code != 200:
                await safe_reply(update.effective_message, f"❌ No pending action found for incident `{incident_id}`.")
                return
            act_data = act_resp.json()
            action_id = act_data.get("action_id", "act-unknown")

            payload = {
                "action_id": action_id,
                "decision": "approved",
                "actor": actor,
                "channel": "telegram",
                "reason": reason,
                **tg_fields,
            }
            resp = await client.post(
                f"{api_url}/v1/incidents/{incident_id}/approve",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 200:
                resolved_actor = _extract_resolved_actor(resp, actor)
                await safe_reply(
                    update.effective_message,
                    f"✅ *Action Approved*\n\n"
                    f"• *Incident:* `{incident_id}`\n"
                    f"• *Action ID:* `{action_id}`\n"
                    f"• *Approved By:* `{resolved_actor}`\n"
                    f"• *Reason:* `{reason}`\n\n"
                    f"Action authorized. State machine advancing to `EXECUTING`.",
                )
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await safe_reply(update.effective_message, f"❌ Approval failed ({resp.status_code}): {detail}")
    except Exception as exc:
        await safe_reply(update.effective_message, f"❌ Error communicating with Aegis API: {exc}")


@authorized_only
async def deny_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deny a pending remediation action for an incident and escalate."""
    if not update.effective_message:
        return

    args = context.args or []
    if not args:
        await safe_reply(update.effective_message, "Usage: `/deny <incident_id> [reason]`")
        return

    incident_id = args[0].strip()
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Manual denial via Telegram"
    actor, tg_fields = _extract_telegram_user(update.effective_user)

    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            act_resp = await client.get(f"{api_url}/v1/incidents/{incident_id}/action", headers=headers)
            if act_resp.status_code != 200:
                await safe_reply(update.effective_message, f"❌ No pending action found for incident `{incident_id}`.")
                return
            act_data = act_resp.json()
            action_id = act_data.get("action_id", "act-unknown")

            payload = {
                "action_id": action_id,
                "decision": "denied",
                "actor": actor,
                "channel": "telegram",
                "reason": reason,
                **tg_fields,
            }
            resp = await client.post(
                f"{api_url}/v1/incidents/{incident_id}/approve",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 200:
                resolved_actor = _extract_resolved_actor(resp, actor)
                await safe_reply(
                    update.effective_message,
                    f"❌ *Action Denied*\n\n"
                    f"• *Incident:* `{incident_id}`\n"
                    f"• *Action ID:* `{action_id}`\n"
                    f"• *Denied By:* `{resolved_actor}`\n"
                    f"• *Reason:* `{reason}`\n\n"
                    f"Action denied. Incident escalated.",
                )
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await safe_reply(update.effective_message, f"❌ Denial failed ({resp.status_code}): {detail}")
    except Exception as exc:
        await safe_reply(update.effective_message, f"❌ Error communicating with Aegis API: {exc}")


@authorized_only
async def drop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop an incident — stop system/agents from working further on it."""
    if not update.effective_message:
        return

    args = context.args or []
    if not args:
        await safe_reply(
            update.effective_message,
            "Usage: `/drop <incident_id> [reason]`",
        )
        return

    incident_id = args[0].strip()
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Dropped via Telegram"
    actor, tg_fields = _extract_telegram_user(update.effective_user)

    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{api_url}/v1/incidents/{incident_id}/drop",
                json={"reason": reason, "actor": actor, "channel": "telegram", **tg_fields},
                headers=headers,
            )
            if resp.status_code == 200:
                resolved_actor = _extract_resolved_actor(resp, actor)
                await safe_reply(
                    update.effective_message,
                    f"🛑 *Incident Dropped*\n\n"
                    f"• *Incident:* `{incident_id}`\n"
                    f"• *Dropped By:* `{resolved_actor}`\n"
                    f"• *Reason:* `{reason}`\n\n"
                    f"System and agents halted from further operations on this incident. State set to `CLOSED`.",
                )
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await safe_reply(update.effective_message, f"❌ Drop failed ({resp.status_code}): {detail}")
    except Exception as exc:
        await safe_reply(update.effective_message, f"❌ Error communicating with Aegis API: {exc}")


@authorized_only
async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-close an incident."""
    if not update.effective_message:
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: `/close <incident_id> [reason]`", parse_mode="Markdown"
        )
        return

    incident_id = args[0].strip()
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Manual close via Telegram"
    actor, tg_fields = _extract_telegram_user(update.effective_user)

    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{api_url}/v1/incidents/{incident_id}/close",
                json={"reason": reason, "actor": actor, "channel": "telegram", **tg_fields},
                headers=headers,
            )
            if resp.status_code == 200:
                await safe_reply(
                    update.effective_message,
                    f"✅ Incident `{incident_id}` closed successfully.",
                )
            else:
                await update.effective_message.reply_text(f"❌ Close failed: HTTP {resp.status_code}")
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ Error: {exc}")


@authorized_only
async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses for action approval/denial/steering."""
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Invalid callback data", show_alert=True)
        return

    action_type, incident_id, action_id = parts[0], parts[1], parts[2]
    actor, tg_fields = _extract_telegram_user(query.from_user)

    if action_type == "steer":
        await query.answer()
        if query.message:
            await query.message.reply_text(
                f"To steer incident `{incident_id}`, reply with:\n`/steer {incident_id} <your guidance>`",
                parse_mode="Markdown",
            )
        return

    decision = "approved" if action_type == "approve" else "denied"

    api_url, headers = _get_api_client(context)

    payload = {
        "action_id": action_id,
        "decision": decision,
        "actor": actor,
        "channel": "telegram",
        **tg_fields,
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{api_url}/v1/incidents/{incident_id}/approve",
                json=payload,
                headers=headers,
            )

            if resp.status_code == 200:
                resolved_actor = _extract_resolved_actor(resp, actor)
                # Successfully approved or denied
                outcome_label = f"Approved by {resolved_actor}" if decision == "approved" else f"Denied by {resolved_actor}"
                icon = "✅" if decision == "approved" else "❌"
                await query.answer(f"Action {decision.capitalize()}!")

                # Edit message to show decision and remove buttons (prevents double-tap)
                if query.message:
                    current_text = query.message.text or ""
                    updated_text = (
                        f"{current_text}\n\n"
                        f"{icon} *{outcome_label}*\n"
                        f"State machine advancing..."
                    )
                    try:
                        await query.edit_message_text(
                            text=updated_text,
                            parse_mode="Markdown",
                            reply_markup=None,
                        )
                    except Exception as exc:
                        if "can't parse entities" in str(exc).lower():
                            try:
                                await query.edit_message_text(
                                    text=updated_text,
                                    parse_mode=None,
                                    reply_markup=None,
                                )
                            except Exception:
                                pass
                        logger.warning("Failed to edit approval message: %s", exc, exc_info=True)
            elif resp.status_code == 409:
                # Same-decision double tap or already decided
                await query.answer("Already decided", show_alert=True)
                if query.message:
                    try:
                        await query.edit_message_reply_markup(reply_markup=None)
                    except Exception as exc:
                        logger.warning("Failed to remove reply markup on 409: %s", exc, exc_info=True)
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await query.answer(f"Error ({resp.status_code}): {detail}", show_alert=True)
    except Exception as exc:
        logger.error("Exception handling approval callback: %s", exc)
        await query.answer(f"Connection error: {exc}", show_alert=True)


@authorized_only
async def approve_drift_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a pending SHA-256 topology or runbooks drift update."""
    if not update.effective_message:
        return
    args = context.args or []
    if not args or args[0].strip().lower() not in ("topology", "runbooks"):
        await safe_reply(update.effective_message, "Usage: `/approve_drift <topology|runbooks>`")
        return

    target = args[0].strip().lower()
    actor, tg_fields = _extract_telegram_user(update.effective_user)
    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{api_url}/v1/drift/{target}/approve",
                json={"actor": actor, "channel": "telegram", **tg_fields},
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                sha_short = str(data.get("sha256", ""))[:12]
                await safe_reply(
                    update.effective_message,
                    f"✅ *Drift Sync Approved (`{target}`)*\n\n"
                    f"• *Version:* `v{data.get('version', 1)}`\n"
                    f"• *SHA-256:* `{sha_short}`\n"
                    f"• *Operator:* `{_extract_resolved_actor(resp, actor)}`",
                )
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await safe_reply(update.effective_message, f"❌ Drift approval failed ({resp.status_code}): {detail}")
    except Exception as exc:
        await safe_reply(update.effective_message, f"❌ Error communicating with Aegis API: {exc}")


@authorized_only
async def reject_drift_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject/dismiss a pending SHA-256 topology or runbooks drift alert."""
    if not update.effective_message:
        return
    args = context.args or []
    if not args or args[0].strip().lower() not in ("topology", "runbooks"):
        await safe_reply(update.effective_message, "Usage: `/reject_drift <topology|runbooks> [reason]`")
        return

    target = args[0].strip().lower()
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Rejected via Telegram"
    actor, tg_fields = _extract_telegram_user(update.effective_user)
    api_url, headers = _get_api_client(context)

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{api_url}/v1/drift/{target}/reject",
                json={"reason": reason, "actor": actor, "channel": "telegram", **tg_fields},
                headers=headers,
            )
            if resp.status_code == 200:
                await safe_reply(
                    update.effective_message,
                    f"🛑 *Drift Rejected (`{target}`)*\n"
                    f"• *Operator:* `{_extract_resolved_actor(resp, actor)}`\n"
                    f"• *Reason:* `{reason}`",
                )
            else:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                await safe_reply(update.effective_message, f"❌ Drift rejection failed ({resp.status_code}): {detail}")
    except Exception as exc:
        await safe_reply(update.effective_message, f"❌ Error communicating with Aegis API: {exc}")
