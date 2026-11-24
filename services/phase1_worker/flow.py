"""CrewAI Flow orchestrating Phase 1 investigator agents.

Flow sequence:
1. @start: launches Agent 1 (Infrastructure), writes infra_flags to Redis.
2. @listen(agent1): launches Agent 2 (Logs/Traces) and Agent 3 (Change/Config) in parallel.
   Agent 2 inspects infra_flags to set search depth.
3. @listen(agent2, agent3): calls merge_findings() and writes MergedFinding to Redis.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
import redis

try:
    from crewai.flow.flow import Flow, start, listen
except ImportError:  # pragma: no cover
    try:
        from crewai.flow import Flow, start, listen
    except ImportError:
        # Fallback decorators if crewai is mocked in lightweight tests
        class Flow:
            pass

        def start():
            def decorator(fn):
                fn._is_start = True
                return fn
            return decorator

        def listen(*args, **kwargs):
            def decorator(fn):
                fn._listens_to = args
                return fn
            return decorator

from services.phase1_worker.agents.change_config import ChangeConfigInvestigator
from services.phase1_worker.agents.infrastructure import InfrastructureInvestigator
from services.phase1_worker.agents.logs_traces import LogTraceInvestigator
from services.phase1_worker.merge import merge_findings
from shared.models.findings import AgentFinding, MergedFinding

logger = logging.getLogger(__name__)


class Phase1Flow(Flow):
    """CrewAI Flow orchestrating the 3 Phase 1 investigator agents."""

    _skip_auto_memory = True

    def __init__(
        self,
        incident_id: str,
        affected_namespace: str = "default",
        affected_service: str = "unknown",
        alert_metadata: Optional[Dict[str, Any]] = None,
        topology_subgraph: Optional[Dict[str, Any]] = None,
        infra_investigator: Optional[InfrastructureInvestigator] = None,
        log_investigator: Optional[LogTraceInvestigator] = None,
        change_investigator: Optional[ChangeConfigInvestigator] = None,
        redis_client: Optional[redis.Redis] = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.incident_id = incident_id
        self.affected_namespace = affected_namespace
        self.affected_service = affected_service
        self.alert_metadata = alert_metadata or {}
        self.topology_subgraph = topology_subgraph or {}
        self.redis_client = redis_client

        self.infra_agent = infra_investigator or InfrastructureInvestigator(redis_client=redis_client)
        self.logs_agent = log_investigator or LogTraceInvestigator(redis_client=redis_client)
        self.change_agent = change_investigator or ChangeConfigInvestigator()

        self.a1_finding: Optional[AgentFinding] = None
        self.a2_finding: Optional[AgentFinding] = None
        self.a3_finding: Optional[AgentFinding] = None
        self.merged_finding: Optional[MergedFinding] = None

    @start()
    async def run_infra_agent(self) -> AgentFinding:
        """Step 1: Launch Agent 1 (Infrastructure) and save infra_flags to Redis."""
        logger.info("[Phase1Flow] Step 1: Running Infrastructure Investigator for %s", self.incident_id)
        self.a1_finding = await self.infra_agent.investigate(
            incident_id=self.incident_id,
            affected_namespace=self.affected_namespace,
            affected_service=self.affected_service,
            alert_metadata=self.alert_metadata,
            topology_subgraph=self.topology_subgraph,
        )

        # Write infra_flags to Redis (incident:{id}:flags)
        if self.redis_client is not None:
            try:
                flags_key = f"incident:{self.incident_id}:flags"
                self.redis_client.set(flags_key, json.dumps(self.a1_finding.infra_flags))
                logger.info("[Phase1Flow] Saved infra_flags to Redis: %s", self.a1_finding.infra_flags)
            except Exception as e:
                logger.warning("[Phase1Flow] Failed to write infra_flags to Redis: %s", e)

        return self.a1_finding

    @listen(run_infra_agent)
    async def run_parallel_investigators(self, a1_finding: AgentFinding) -> tuple[AgentFinding, AgentFinding]:
        """Step 2: Launch Agent 2 and Agent 3 concurrently.
        
        Agent 2 inspects infra_flags to decide search depth.
        """
        logger.info("[Phase1Flow] Step 2: Running Logs & Change Investigators in parallel for %s", self.incident_id)
        infra_flags = a1_finding.infra_flags if a1_finding else {}

        task_a2 = self.logs_agent.investigate(
            incident_id=self.incident_id,
            affected_namespace=self.affected_namespace,
            affected_service=self.affected_service,
            infra_flags=infra_flags,
            alert_metadata=self.alert_metadata,
            topology_subgraph=self.topology_subgraph,
        )

        alert_timestamp = None
        if self.alert_metadata and isinstance(self.alert_metadata, dict):
            primary = self.alert_metadata.get("primary_alert")
            if isinstance(primary, dict):
                starts_at = primary.get("startsAt")
                if starts_at and isinstance(starts_at, str):
                    try:
                        from datetime import datetime
                        alert_timestamp = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                    except Exception:
                        pass

        task_a3 = self.change_agent.investigate(
            incident_id=self.incident_id,
            affected_namespace=self.affected_namespace,
            affected_service=self.affected_service,
            alert_timestamp=alert_timestamp,
            alert_metadata=self.alert_metadata,
            topology_subgraph=self.topology_subgraph,
        )

        self.a2_finding, self.a3_finding = await asyncio.gather(task_a2, task_a3)
        return self.a2_finding, self.a3_finding

    @listen(run_parallel_investigators)
    async def merge_results(self, findings_tuple: tuple[AgentFinding, AgentFinding]) -> MergedFinding:
        """Step 3: Programmatically merge all 3 findings and write to Redis."""
        logger.info("[Phase1Flow] Step 3: Merging findings for %s", self.incident_id)
        all_findings = []
        if self.a1_finding:
            all_findings.append(self.a1_finding)
        if self.a2_finding:
            all_findings.append(self.a2_finding)
        if self.a3_finding:
            all_findings.append(self.a3_finding)

        self.merged_finding = merge_findings(all_findings, incident_id=self.incident_id)

        # Write MergedFinding to incident:{id}:context in Redis
        if self.redis_client is not None:
            try:
                context_key = f"incident:{self.incident_id}:context"
                self.redis_client.set(context_key, self.merged_finding.model_dump_json())
                logger.info("[Phase1Flow] MergedFinding saved to Redis at %s", context_key)
            except Exception as e:
                logger.error("[Phase1Flow] Failed to write MergedFinding to Redis: %s", e)

        return self.merged_finding

    async def run(self) -> MergedFinding:
        """Execute the flow with full parallelism and return the final MergedFinding.
        
        Agent 3 (Change & Config) runs in parallel from T=0 without waiting for Agent 1.
        Agent 1 (Infrastructure) runs with a head-start to establish platform ground-truth
        and record infra_flags to Redis for Agent 2 (Log & Trace).
        Agent 2 and Agent 3 resolve concurrently, and all findings are merged deterministically.
        """
        # Launch Agent 3 (Change & Config) immediately in parallel at T=0
        task_a3 = asyncio.create_task(self.change_agent.investigate(
            incident_id=self.incident_id,
            affected_namespace=self.affected_namespace,
            affected_service=self.affected_service,
            alert_metadata=self.alert_metadata,
            topology_subgraph=self.topology_subgraph,
        ))

        try:
            # Run Agent 1 (Infrastructure) with head-start
            a1 = await self.run_infra_agent()

            # Run Agent 2 (Log & Trace) with infra_flags from Agent 1
            infra_flags = a1.infra_flags if a1 else {}
            task_a2 = self.logs_agent.investigate(
                incident_id=self.incident_id,
                affected_namespace=self.affected_namespace,
                affected_service=self.affected_service,
                infra_flags=infra_flags,
                alert_metadata=self.alert_metadata,
                topology_subgraph=self.topology_subgraph,
            )

            # Await parallel completion of Agent 2 and Agent 3
            self.a2_finding, self.a3_finding = await asyncio.gather(task_a2, task_a3)
            merged = await self.merge_results((self.a2_finding, self.a3_finding))
            return merged
        except Exception:
            if not task_a3.done():
                task_a3.cancel()
            raise

