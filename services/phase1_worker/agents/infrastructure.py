"""Agent 1: Infrastructure Investigator.

Responsible for resource saturation, pod lifecycle, and cluster health analysis.
Writes infra_flags to Redis for Agent 2.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import redis


from langchain_core.language_models.chat_models import BaseChatModel
from services.phase1_worker.agents.base import BaseInvestigator
from shared.harness.mcp import MCPClient
from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding

logger = logging.getLogger(__name__)


class InfrastructureInvestigator(BaseInvestigator):
    """Investigates infrastructure-level resource saturation and pod conditions."""

    default_mcp_servers: List[str] = ["prometheus", "kubernetes"]

    def __init__(
        self,
        llm: Optional[BaseChatModel] = None,
        mcp_clients: Optional[Dict[str, MCPClient]] = None,
        redis_client: Optional[redis.Redis] = None,
        token_budget: Optional[int] = None,
        max_iter: Optional[int] = None,
        max_tool_calls: Optional[int] = None,
        prompts_dir: Optional[Union[str, Path]] = None,
    ):
        super().__init__(
            agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
            agent_config_key="infrastructure_investigator",
            task_config_key="investigate_infrastructure",
            llm=llm,
            mcp_clients=mcp_clients,
            token_budget=token_budget,
            max_iter=max_iter,
            max_tool_calls=max_tool_calls,
            prompts_dir=prompts_dir,
        )
        self.redis_client = redis_client


    async def investigate(
        self,
        incident_id: str,
        affected_namespace: str = "default",
        affected_service: str = "unknown",
        alert_metadata: Optional[Dict[str, Any]] = None,
        topology_subgraph: Optional[Dict[str, Any]] = None,
    ) -> AgentFinding:
        """Run the Infrastructure Investigator workflow."""
        logger.info("[%s] Starting investigation for incident %s", self.agent_role.value, incident_id)

        context_vars = {
            "service": affected_service,
            "namespace": affected_namespace,
            "alert_context": json.dumps(alert_metadata or {}),
            "topology_subgraph": json.dumps(topology_subgraph or {}),
            "severity": alert_metadata.get("severity", "P3") if alert_metadata else "P3"
        }

        # Let the CrewAI Agent autonomously execute tools and reason
        finding = await self._execute_crew(context_vars)

        # Write infra_flags to Redis (incident:{id}:flags)
        if self.redis_client is not None:
            try:
                flags_key = f"incident:{incident_id}:flags"
                self.redis_client.set(flags_key, json.dumps(finding.infra_flags))
                logger.info("[%s] Written infra_flags to %s: %s", self.agent_role.value, flags_key, finding.infra_flags)
            except Exception as e:
                logger.warning("[%s] Failed to write infra_flags to Redis: %s", self.agent_role.value, e)

        return finding
