"""Telegram notification formatter and dispatcher for Aegis incident events."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

logger = logging.getLogger("aegis.telegram_bot.notifier")


def _clean_md(text: Any) -> str:
    """Escape markdown v1 special characters in untrusted variable text."""
    if not text:
        return ""
    return escape_markdown(str(text), version=1)


class TelegramNotifier:
    """Handles formatting and dispatching of Aegis incident notifications to Telegram."""

    def __init__(self, bot: Optional[Any] = None, default_chat_id: Optional[str] = None):
        self.bot = bot
        self.default_chat_id = default_chat_id or ""
        # In-memory mapping of action_id -> (chat_id, message_id) for dynamic message editing
        self.tracked_actions: Dict[str, Tuple[str, int]] = {}
        # Cached approval text to allow appending decisions on edit
        self.action_texts: Dict[str, str] = {}

    def _resolve_chat_id(self, chat_id: Optional[str]) -> str:
        return chat_id or self.default_chat_id

    async def _send_safe_message(self, chat_id: str, text: str, **kwargs) -> Optional[Any]:
        """Send message with Markdown parse_mode, falling back to plain text if Telegram entity parsing fails."""
        if not self.bot or not chat_id:
            return None
        try:
            return await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                **kwargs,
            )
        except Exception as exc:
            if "can't parse entities" in str(exc).lower():
                logger.warning("Markdown parse failed in notifier (%s); sending plain text fallback", exc)
                clean_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=None,
                    **clean_kwargs,
                )
            logger.error("Failed to send message: %s", exc)
            return None

    async def send_incident_created(self, incident: Dict[str, Any], chat_id: Optional[str] = None) -> Optional[Any]:
        """Send formatted alert when an incident is created."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_incident_created.")
            return None

        incident_id = incident.get("incident_id", "Unknown")
        severity = incident.get("severity", "P3").upper()
        service = incident.get("affected_service", "Unknown")
        namespace = incident.get("affected_namespace", "default")
        created_at = incident.get("created_at", "")

        sev_emoji = "🚨" if severity == "P1" else ("⚠️" if severity == "P2" else "ℹ️")

        text = (
            f"{sev_emoji} *Aegis Incident Created*\n\n"
            f"• *ID:* `{incident_id}`\n"
            f"• *Severity:* *{severity}*\n"
            f"• *Service:* `{service}`\n"
            f"• *Namespace:* `{namespace}`\n"
            f"• *State:* `NEW` → `GROUPING`\n"
            f"• *Time:* `{created_at}`\n\n"
            f"Investigation pipeline started. Triage in progress..."
        )

        return await self._send_safe_message(target_chat, text)

    async def send_triage_summary(
        self, incident: Dict[str, Any], merged_findings: Dict[str, Any], chat_id: Optional[str] = None
    ) -> Optional[Any]:
        """Send formatted triage summary with root cause hypotheses and top 3 evidence items."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_triage_summary.")
            return None

        incident_id = incident.get("incident_id", merged_findings.get("incident_id", "Unknown"))
        service = incident.get("affected_service", "Unknown")
        hyp_str = (
            merged_findings.get("primary_hypothesis")
            or (merged_findings.get("root_cause_hypotheses", ["Automated analysis underway."])[0] if merged_findings.get("root_cause_hypotheses") else "Automated analysis underway.")
        )
        evidence_items = merged_findings.get("all_evidence") or merged_findings.get("evidence", [])

        # Extract top 3 evidence items
        top_evidence = evidence_items[:3]
        evidence_lines = []
        for i, ev in enumerate(top_evidence, 1):
            if isinstance(ev, dict):
                src = _clean_md(ev.get("source_tool") or ev.get("source", "telemetry"))
                desc = _clean_md(ev.get("raw_output") or ev.get("query_or_cmd") or ev.get("description", str(ev)))
                evidence_lines.append(f"{i}. *[{src}]* {desc}")
            else:
                evidence_lines.append(f"{i}. {_clean_md(ev)}")

        evidence_str = "\n".join(evidence_lines) if evidence_lines else "• Telemetry and log correlation captured."

        severity = incident.get("severity", "P2").upper()
        sev_badge = "🚨 P1" if severity == "P1" else ("⚠️ P2" if severity == "P2" else "ℹ️ P3")

        text = (
            f"🔍 *Triage Summary: {incident_id}*\n\n"
            f"• *Target Service:* `{service}` ({sev_badge})\n"
            f"• *Primary Hypothesis:*\n{_clean_md(hyp_str)}\n\n"
            f"📋 *Top Evidence:*\n{evidence_str}\n\n"
            f"Proceeding to Remediation Planning..."
        )

        return await self._send_safe_message(target_chat, text)

    async def send_approval_request(
        self, incident: Dict[str, Any], action: Dict[str, Any], chat_id: Optional[str] = None
    ) -> Optional[Any]:
        """Send formatted approval request with action details and inline Approve/Deny/Steer buttons."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_approval_request.")
            return None

        incident_id = incident.get("incident_id", action.get("incident_id", "Unknown"))
        action_id = action.get("action_id")
        if not action_id:
            logger.error("Missing action_id in approval request: %s", action)
            return None

        proposal = action.get("proposal") or action
        confidence = proposal.get("confidence", 0.0)
        risk = proposal.get("risk_assessment", "Standard operational action.")

        # Actions list from proposal
        actions_list = proposal.get("actions", [])
        action_desc = "No action steps provided."
        if actions_list:
            first_act = actions_list[0]
            if isinstance(first_act, dict):
                tool = first_act.get("tool_name", "tool")
                res = first_act.get("target_resource", "resource")
                params = first_act.get("parameters", {})
                if params:
                    param_str = ", ".join(f"{k}={v}" for k, v in params.items())
                    action_desc = f"`{tool}` on `{res}` (`{param_str}`)"
                else:
                    action_desc = f"`{tool}` on `{res}`"
            else:
                action_desc = f"`{first_act}`"

        service = incident.get("affected_service") or incident.get("service") or proposal.get("affected_service", "Unknown")
        severity = incident.get("severity", "P2").upper()
        sev_emoji = "🚨" if severity == "P1" else ("⚠️" if severity == "P2" else "ℹ️")

        service_line = f"• *Service:* `{service}`\n" if service and service != "Unknown" else ""
        severity_line = f"• *Severity:* {sev_emoji} *{severity}*\n" if severity else ""

        raw_rationale = proposal.get("rationale") or (
            actions_list[0].get("rationale") if actions_list and isinstance(actions_list[0], dict) else ""
        )
        rationale = str(raw_rationale or "").split("\n\n[Runbook Reference]")[0].split("[Runbook Ref:")[0].strip()
        rationale_line = f"• *Rationale:* {_clean_md(rationale)}\n" if rationale else ""

        citations = (
            proposal.get("runbook_citations")
            or action.get("runbook_citations")
            or (actions_list[0].get("runbook_citations") if actions_list and isinstance(actions_list[0], dict) else [])
            or []
        )
        citation_block = ""
        if isinstance(citations, list) and citations:
            cit_lines: List[str] = ["\n📚 *Runbook Citations:*"]
            for idx, cit in enumerate(citations[:3], start=1):
                if not isinstance(cit, dict):
                    cit_lines.append(f"{idx}. {_clean_md(cit)}")
                    continue
                b_name = str(cit.get("book_name") or cit.get("runbook_id") or "Runbook").strip()
                r_id = str(cit.get("runbook_id") or "").strip()
                sec = str(cit.get("section") or "General").strip()
                l_range = str(cit.get("line_range") or "").strip()
                c_id = str(cit.get("chunk_id") or "").strip()
                excerpt = str(cit.get("referenced_text") or "").strip()
                id_title = (
                    f"{r_id}/{b_name}"
                    if (r_id and b_name and r_id.lower() != b_name.lower())
                    else (r_id or b_name)
                )
                sec_loc = f"{sec}/{l_range}" if l_range else sec
                if c_id:
                    sec_loc += f" ({c_id})"
                line_text = (
                    f"*{idx}.*\n"
                    f"  • `id/title:` '{_clean_md(id_title)}'\n"
                    f"  • `section/line_range:` '{_clean_md(sec_loc)}'"
                )
                if excerpt:
                    short_ex = excerpt if len(excerpt) <= 180 else excerpt[:177] + "..."
                    line_text += f'\n  • `reference:` "{_clean_md(short_ex)}"'
                cit_lines.append(line_text)
            citation_block = "\n".join(cit_lines) + "\n"

        text = (
            f"🛡️ *ACTION APPROVAL REQUIRED*\n\n"
            f"• *Incident:* `{incident_id}`\n"
            f"{service_line}"
            f"{severity_line}"
            f"• *Action ID:* `{action_id}`\n"
            f"• *Proposed Operation:* {action_desc}\n"
            f"{rationale_line}"
            f"• *Confidence Score:* `{confidence:.2f}`\n"
            f"• *Risk Assessment:* {_clean_md(risk)}\n"
            f"{citation_block}\n"
            f"⚡ Human authorization is required to proceed."
        )

        cb_approve = f"approve:{incident_id}:{action_id}"
        cb_deny = f"deny:{incident_id}:{action_id}"
        cb_steer = f"steer:{incident_id}:{action_id}"
        assert len(cb_approve.encode("utf-8")) <= 64, f"callback_data too long: {cb_approve}"
        assert len(cb_deny.encode("utf-8")) <= 64, f"callback_data too long: {cb_deny}"
        assert len(cb_steer.encode("utf-8")) <= 64, f"callback_data too long: {cb_steer}"

        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=cb_approve),
                InlineKeyboardButton("❌ Deny", callback_data=cb_deny),
            ],
            [
                InlineKeyboardButton("🔄 Steer Plan", callback_data=cb_steer),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await self._send_safe_message(
            target_chat,
            text,
            reply_markup=reply_markup,
        )
        if msg and hasattr(msg, "message_id"):
            self.tracked_actions[action_id] = (str(target_chat), msg.message_id)
            self.action_texts[action_id] = text
        return msg

    async def send_denial_notification(
        self,
        incident: Dict[str, Any],
        action_id: str,
        denied_by: str,
        channel: str = "dashboard",
        reason: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Notify Telegram of a cross-channel denial (or update open approval message)."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_denial_notification.")
            return None

        incident_id = incident.get("incident_id", "Unknown")

        # Check if we have an open approval message to edit dynamically
        if action_id in self.tracked_actions:
            chat, msg_id = self.tracked_actions[action_id]
            base_text = self.action_texts.get(action_id, f"🛡️ *ACTION APPROVAL: {incident_id}*")
            updated_text = (
                f"{base_text}\n\n"
                f"❌ *Denied via {channel.capitalize()} by {denied_by}*\n"
                f"Action permanently blocked. Incident transitioned to `ESCALATED`."
            )
            try:
                edited = await self.bot.edit_message_text(
                    chat_id=chat,
                    message_id=msg_id,
                    text=updated_text,
                    parse_mode="Markdown",
                    reply_markup=None,  # remove inline buttons
                )
                return edited
            except Exception as exc:
                logger.warning("Could not edit tracked approval message: %s; posting new notification.", exc)

        # Fallback if untracked or edit failed
        reason_str = f"\n• *Reason:* {_clean_md(reason)}" if reason else ""
        text = (
            f"❌ *Action Denied ({channel.capitalize()})*\n\n"
            f"• *Incident:* `{incident_id}`\n"
            f"• *Action ID:* `{action_id}`\n"
            f"• *Denied By:* `{denied_by}` ({channel}){reason_str}\n\n"
            f"Action permanently blocked per Rule 9. Incident escalated."
        )

        return await self._send_safe_message(target_chat, text)

    async def send_escalation(
        self, incident: Dict[str, Any], reason: str, chat_id: Optional[str] = None
    ) -> Optional[Any]:
        """Send notification when an incident transitions to ESCALATED."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_escalation.")
            return None

        incident_id = incident.get("incident_id", "Unknown")
        service = incident.get("affected_service", "Unknown")
        severity = incident.get("severity", "P2").upper()
        sev_badge = "🚨 P1" if severity == "P1" else ("⚠️ P2" if severity == "P2" else "ℹ️ P3")

        text = (
            f"⚠️ *INCIDENT ESCALATED*\n\n"
            f"• *Incident:* `{incident_id}` ({sev_badge})\n"
            f"• *Service:* `{service}`\n"
            f"• *Reason:* {_clean_md(reason)}\n\n"
            f"Human SRE intervention required.\n"
            f"💡 *Action:* Run `/steer {incident_id} <instructions>` to provide guidance or review in Dashboard."
        )

        return await self._send_safe_message(target_chat, text)

    async def send_report(
        self, incident: Dict[str, Any], report_path: str, chat_id: Optional[str] = None
    ) -> Optional[Any]:
        """Send the generated postmortem report as a document attachment (or message)."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_report.")
            return None

        incident_id = incident.get("incident_id", "Unknown")
        service = incident.get("affected_service", "Unknown")
        severity = incident.get("severity", "P2").upper()
        p = Path(report_path)

        if p.exists() and p.is_file():
            caption = (
                f"📄 *Aegis Post-Incident Report: {incident_id}*\n"
                f"• *Service:* `{service}` ({severity})\n"
                f"• *Status:* `CLOSED`"
            )
            try:
                with open(p, "rb") as f:
                    return await self.bot.send_document(
                        chat_id=target_chat,
                        document=f,
                        filename=p.name,
                        caption=caption,
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.error("Failed to send report document: %s", exc)
                return None
        else:
            # Fallback if file not on shared path
            text = (
                f"📄 *Aegis Incident Closed: {incident_id}*\n\n"
                f"Post-incident report generated at `{report_path}`.\n"
                f"State: `CLOSED`."
            )
            return await self._send_safe_message(target_chat, text)

    async def dispatch(self, event: str, incident_id: str, data: Dict[str, Any]) -> bool:
        """Route notification payload to the appropriate handler."""
        incident = data.get("incident", {"incident_id": incident_id})

        if event == "incident_created":
            res = await self.send_incident_created(incident)
            return res is not None
        elif event == "triage_summary":
            findings = data.get("merged_findings", {})
            res = await self.send_triage_summary(incident, findings)
            return res is not None
        elif event == "approval_request":
            action = data.get("action", {})
            res = await self.send_approval_request(incident, action)
            return res is not None
        elif event == "denial_notification":
            action_id = data.get("action_id", "")
            actor = data.get("actor", "operator")
            channel = data.get("channel", "dashboard")
            reason = data.get("reason")
            res = await self.send_denial_notification(incident, action_id, actor, channel, reason)
            return res is not None
        elif event == "escalation":
            reason = data.get("reason", "Autonomous pipeline escalated.")
            res = await self.send_escalation(incident, reason)
            return res is not None
        elif event == "report_delivered":
            from shared.config.loader import load_system_config
            try:
                storage = load_system_config().ledger.storage_path
            except Exception:
                storage = "/data/incidents"
            path = data.get("report_path", f"{storage}/{incident_id}/report.md")
            res = await self.send_report(incident, path)
            return res is not None
        elif event == "drift_detected":
            res = await self.send_drift_detected(data)
            return res is not None
        else:
            logger.warning("Unrecognized notification event: %s", event)
            return False

    async def send_drift_detected(self, drift: Dict[str, Any], chat_id: Optional[str] = None) -> Optional[Any]:
        """Alert operator via Telegram when SHA-256 topology or runbook drift is detected."""
        target_chat = self._resolve_chat_id(chat_id)
        if not self.bot or not target_chat:
            logger.warning("Bot or chat_id unconfigured; skipping send_drift_detected.")
            return None

        target = str(drift.get("target", "unknown"))
        old_sha = str(drift.get("old_sha") or "none")[:12]
        new_sha = str(drift.get("new_sha") or "unknown")[:12]
        cur_ver = drift.get("current_version", 0)
        summary = drift.get("summary") or {}

        detail_lines: List[str] = []
        if target == "topology":
            added = summary.get("added_nodes") or []
            removed = summary.get("removed_nodes") or []
            changed = summary.get("changed_nodes") or []
            if added:
                detail_lines.append(f"• *Added Nodes:* `{', '.join(added[:8])}`")
            if removed:
                detail_lines.append(f"• *Removed Nodes:* `{', '.join(removed[:8])}`")
            if changed:
                detail_lines.append(f"• *Changed Nodes:* `{', '.join(changed[:8])}`")
        else:
            added = summary.get("added_runbooks") or []
            removed = summary.get("removed_runbooks") or []
            if added:
                detail_lines.append(f"• *Added Runbooks:* `{', '.join(added[:8])}`")
            if removed:
                detail_lines.append(f"• *Removed Runbooks:* `{', '.join(removed[:8])}`")
            detail_lines.append(f"• *Live Index Entries:* `{summary.get('live_entry_count', 0)}`")

        details_block = ("\n" + "\n".join(detail_lines)) if detail_lines else ""
        text = (
            f"🔄 *Aegis Drift Detected: `{target}`*\n\n"
            f"• *Active Version:* `v{cur_ver}`\n"
            f"• *SHA-256:* `{old_sha}` → `{new_sha}`"
            f"{details_block}\n\n"
            f"Snapshot/index was *not* overwritten automatically.\n"
            f"Run `/approve_drift {target}` to sync or `/reject_drift {target} [reason]` to dismiss."
        )
        return await self._send_safe_message(target_chat, text)
