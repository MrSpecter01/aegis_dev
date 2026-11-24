"""Agent 2: Log & Trace Investigator.

Analyzes application logs and distributed traces.
Search depth is guided by infra_flags from Agent 1.
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


class LogTraceInvestigator(BaseInvestigator):
    """Investigates application logs via Loki and distributed traces via Tempo."""

    default_mcp_servers: List[str] = ["loki", "tempo"]

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
            agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
            agent_config_key="log_trace_investigator",
            task_config_key="investigate_logs_traces",
            llm=llm,
            mcp_clients=mcp_clients,
            token_budget=token_budget,
            max_iter=max_iter,
            max_tool_calls=max_tool_calls,
            prompts_dir=prompts_dir,
        )
        self.redis_client = redis_client


    def _read_infra_flags(self, incident_id: str) -> Dict[str, Any]:
        """Read infra_flags written by Agent 1 from Redis."""
        if not self.redis_client or not isinstance(self.redis_client, redis.Redis):
            return {}
        try:
            raw = self.redis_client.get(f"incident:{incident_id}:flags")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("[%s] Failed to read infra_flags from Redis: %s", self.agent_role.value, e)
        return {}

    async def investigate(
        self,
        incident_id: str,
        affected_namespace: str = "default",
        affected_service: str = "unknown",
        infra_flags: Optional[Dict[str, Any]] = None,
        alert_metadata: Optional[Dict[str, Any]] = None,
        topology_subgraph: Optional[Dict[str, Any]] = None,
    ) -> AgentFinding:
        """Run the Log & Trace Investigator workflow."""
        logger.info("[%s] Starting investigation for incident %s", self.agent_role.value, incident_id)

        # 1. Resolve infra_flags (from param or Redis)
        flags = infra_flags or self._read_infra_flags(incident_id)

        # Log for DoD verification (softly inferred depth)
        if flags.get("oom_killed") or flags.get("node_eviction"):
            logger.info("search_depth=shallow (guided)")
        else:
            logger.info("search_depth=deep (guided)")

        context_vars = {
            "service": affected_service,
            "namespace": affected_namespace,
            "alert_context": json.dumps(alert_metadata or {}),
            "topology_subgraph": json.dumps(topology_subgraph or {}),
            "severity": alert_metadata.get("severity", "P3") if alert_metadata else "P3",
            "infra_flags": json.dumps(flags)
        }

        # Let the CrewAI Agent autonomously execute tools and reason
        finding = await self._execute_crew(context_vars)

        return finding
