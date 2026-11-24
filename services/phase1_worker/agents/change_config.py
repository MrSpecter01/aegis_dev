"""Agent 3: Change & Config Investigator.

Correlates recent git commits, ArgoCD deployments, and ConfigMap diffs with incident timing.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


from langchain_core.language_models.chat_models import BaseChatModel
from services.phase1_worker.agents.base import BaseInvestigator
from shared.harness.mcp import MCPClient
from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding

logger = logging.getLogger(__name__)


class ChangeConfigInvestigator(BaseInvestigator):
    """Investigates code changes, release syncs, and config alterations."""

    default_mcp_servers: List[str] = ["github", "argocd", "kubernetes", "featureflags"]

    def __init__(
        self,
        llm: Optional[BaseChatModel] = None,
        mcp_clients: Optional[Dict[str, MCPClient]] = None,
        token_budget: Optional[int] = None,
        max_iter: Optional[int] = None,
        max_tool_calls: Optional[int] = None,
        prompts_dir: Optional[Union[str, Path]] = None,
    ):
        super().__init__(
            agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
            agent_config_key="change_config_investigator",
            task_config_key="investigate_changes_configs",
            llm=llm,
            mcp_clients=mcp_clients,
            token_budget=token_budget,
            max_iter=max_iter,
            max_tool_calls=max_tool_calls,
            prompts_dir=prompts_dir,
        )


    async def investigate(
        self,
        incident_id: str,
        affected_namespace: str = "default",
        affected_service: str = "unknown",
        alert_timestamp: Optional[datetime] = None,
        alert_metadata: Optional[Dict[str, Any]] = None,
        topology_subgraph: Optional[Dict[str, Any]] = None,
    ) -> AgentFinding:
        """Run the Change & Config Investigator workflow."""
        logger.info("[%s] Starting investigation for incident %s", self.agent_role.value, incident_id)

        ref_time = alert_timestamp or datetime.now(timezone.utc)
        since_dt = ref_time - timedelta(minutes=30)
        until_dt = ref_time + timedelta(minutes=5)
        since_time = since_dt.isoformat()
        until_time = until_dt.isoformat()

        context_vars = {
            "service": affected_service,
            "namespace": affected_namespace,
            "alert_context": json.dumps(alert_metadata or {}),
            "topology_subgraph": json.dumps(topology_subgraph or {}),
            "severity": alert_metadata.get("severity", "P3") if alert_metadata else "P3",
            "since_time": since_time,
            "until_time": until_time
        }

        # Let the CrewAI Agent autonomously execute tools and reason
        finding = await self._execute_crew(context_vars)

        return finding
