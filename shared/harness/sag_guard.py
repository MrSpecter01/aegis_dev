import logging
import uuid
from typing import Any, Dict, Optional, Set

from shared.sag.check_approval import check_approval_status
from shared.sag.validator import check_injection
from shared.harness.mcp import MCPClient
from shared.models.enums import IncidentState
from shared.models.ledger import LedgerEntry
from shared.ledger.writer import append_ledger_entry

logger = logging.getLogger(__name__)

class SAGActionBlockedError(Exception):
    """Raised when an action is permanently blocked or was explicitly denied."""


class SAGApprovalRequiredError(Exception):
    """Raised when a mutating action requires human approval before proceeding."""


class SAGGuard:
    """
    In-process Safety Action Gateway (SAG) PreToolUse Guard.
    
    Wraps tool invocation in the agent harness to enforce:
    1. Early rejection of prompt injection and syntax escapes
    2. Verification that write tools have 'approved: true' in Redis before dispatch
    3. Immutable ledger recording when an action is blocked or denied
    """

    def __init__(
        self,
        mcp_client: Optional[MCPClient] = None,
        redis_client: Optional[Any] = None,
    ):
        self.mcp_client = mcp_client
        self.redis_client = redis_client

    def is_write_tool(self, tool_name: str) -> bool:
        """Determine whether a tool is a mutating/write operation based on policy config."""
        from shared.config.loader import ConfigManager
        cfg = ConfigManager().sag  # config failure raises -> call blocked
        for key, policy in cfg.tools.items():
            if tool_name == key or tool_name.endswith(f"_{key}"):
                return policy.requires_approval
        return getattr(cfg, "default_requires_approval", True)

    def _record_blocked_to_ledger(
        self,
        incident_id: str,
        tool_name: str,
        action_id: str,
        reason: str,
    ) -> None:
        """Write block event into the incident ledger."""
        try:
            entry = LedgerEntry(
                entry_id=f"led-sag-block-{uuid.uuid4().hex[:8]}",
                incident_id=incident_id,
                state=IncidentState.AWAITING_APPROVAL,
                event_type="sag_pretooluse_blocked",
                source="sag_guard",
                details={
                    "tool": tool_name,
                    "action_id": action_id,
                    "reason": reason,
                    "action": "dispatch_aborted",
                },
            )
            append_ledger_entry(self.redis_client, incident_id, entry)
        except Exception as exc:
            # Audit log recording failure should not mask the security block
            logger.warning("Failed to record blocked entry to ledger for incident %s: %s", incident_id, exc, exc_info=True)

    async def pre_tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        incident_id: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> None:
        """
        Execute PreToolUse verification before sending the tool call to FastMCP.
        Raises SAGActionBlockedError if denied.
        Raises SAGApprovalRequiredError if pending or unapproved.
        """
        if not self.is_write_tool(tool_name):
            return

        # 1. Parameter injection and charset verification
        check_injection(arguments)

        # 2. Extract identifiers
        inc_id = incident_id or arguments.get("incident_id")
        act_id = action_id or arguments.get("action_id")

        if not inc_id or not act_id:
            raise SAGApprovalRequiredError(
                f"Write tool '{tool_name}' requires both 'incident_id' and 'action_id' for SAG authorization."
            )

        # Global system emergency brake check
        if self.redis_client is not None:
            try:
                import asyncio, json
                raw_sys = await self.redis_client.get("system:status:paused") if asyncio.iscoroutinefunction(getattr(self.redis_client, "get", None)) else self.redis_client.get("system:status:paused")
                if raw_sys:
                    if isinstance(raw_sys, (bytes, bytearray)):
                        raw_sys = raw_sys.decode("utf-8")
                    sys_obj = json.loads(raw_sys)
                    if sys_obj.get("paused"):
                        reason = sys_obj.get("reason", "Global system pause active")
                        try:
                            self._record_blocked_to_ledger(inc_id, tool_name, act_id, f"Global system pause active: {reason}")
                        except Exception as exc:
                            logger.warning("Failed to record global pause block to ledger: %s", exc, exc_info=True)
                        raise SAGActionBlockedError(f"System is in global standby. Write tool '{tool_name}' blocked: {reason}")
            except SAGActionBlockedError:
                raise
            except Exception as exc:
                logger.warning("Failed to verify global pause status in SAG guard: %s", exc, exc_info=True)

        # 3. Check Redis approval status
        status_code = await check_approval_status(self.redis_client, inc_id, act_id, tool_name=tool_name)

        if status_code == 0:
            # Approved - allow tool dispatch to proceed
            return
        elif status_code == 1:
            # Denied or blocked
            self._record_blocked_to_ledger(
                incident_id=inc_id,
                tool_name=tool_name,
                action_id=act_id,
                reason="action_denied",
            )
            raise SAGActionBlockedError(
                f"Action '{act_id}' for tool '{tool_name}' was DENIED. Execution blocked permanently."
            )
        else:
            # Pending (2) or missing
            self._record_blocked_to_ledger(
                incident_id=inc_id,
                tool_name=tool_name,
                action_id=act_id,
                reason="approval_pending_or_missing",
            )
            raise SAGApprovalRequiredError(
                f"Action '{act_id}' for tool '{tool_name}' requires human approval before execution. Status: pending."
            )

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        incident_id: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Intercept and dispatch tool call."""
        await self.pre_tool_call(tool_name, arguments, incident_id, action_id)

        if self.mcp_client is None:
            raise RuntimeError("MCPClient is not configured on SAGGuard")

        return await self.mcp_client.call_tool(tool_name, arguments)
