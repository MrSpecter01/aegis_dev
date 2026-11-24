"""Phase 2 Remediation Worker main service entry point.

Consumes remediation jobs from Redis Streams (stream:phase2:jobs),
runs the LangGraph Phase 2 Remediation Graph, persists ActionProposal to Redis,
dispatches approved executions, monitors convergence, and drives state machine transitions.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any, Dict, List, Optional

import httpx
import redis

from services.api_server.incident_store import get_incident
from services.phase2_worker.graph import create_remediation_graph, Phase2GraphState
from services.phase2_worker.runbook_index import load_runbooks_index
from shared.audit.tracker import (
    reset_audit_context,
    set_audit_context,
    set_audit_redis_client,
)
from shared.config.loader import get_config
from shared.versioning import RUNBOOKS_META_KEY, read_meta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("phase2_worker")

STREAM_NAME = "stream:phase2:jobs"
CONSUMER_GROUP = "phase2-workers"
CONSUMER_NAME = "worker-1"


class Phase2WorkerService:
    """Worker daemon listening to stream:phase2:jobs."""

    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        graph: Optional[Any] = None,
    ):
        self.config = get_config()
        redis_url = os.getenv("REDIS_URL") or self.config.system.redis.url
        self.redis = redis_client or redis.from_url(redis_url, decode_responses=True)
        set_audit_redis_client(self.redis)
        self.api_url = (
            api_url
            or os.getenv("AEGIS_INTERNAL_API_URL")
            or os.getenv("AEGIS_API_URL")
            or self.config.system.system.api_url
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else (os.getenv("AEGIS_API_KEY") or self.config.system.system.api_key)
        from langgraph.checkpoint.memory import MemorySaver
        self.checkpointer = MemorySaver()
        self.graph = graph or create_remediation_graph(
            checkpointer=self.checkpointer,
            redis_client=self.redis,
            transition_fn=self._post_transition,
        )
        self.runbook_index = load_runbooks_index()
        self._runbook_index_sha = str(read_meta(self.redis, RUNBOOKS_META_KEY).get("sha256") or "")
        self.running = False

    def init_stream_group(self) -> None:
        """Ensure consumer group exists on the job stream."""
        try:
            self.redis.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("Created consumer group '%s' on '%s'", CONSUMER_GROUP, STREAM_NAME)
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug("Consumer group '%s' already exists", CONSUMER_GROUP)
            else:
                logger.error("Error creating stream group: %s", e)
                raise

    async def _post_transition(self, incident_id: str, event: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Notify the Aegis Control Plane API of a state transition."""
        url = f"{self.api_url}/v1/incidents/{incident_id}/transition"
        payload = {
            "event": event,
            "actor": "phase2_worker",
            "metadata": metadata or {},
        }
        headers = {
            "Content-Type": "application/json",
            "X-Aegis-Service": "phase2-worker",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    logger.info("Transitioned incident %s via event '%s': %s", incident_id, event, resp.json())
                    return True
                else:
                    logger.warning("Failed transition for %s (status %d): %s", incident_id, resp.status_code, resp.text)
                    return False
        except Exception as e:
            logger.error("Error calling API transition endpoint for %s: %s", incident_id, e)
            return False

    async def process_job(self, msg_id: str, data: Dict[str, Any]) -> None:
        """Process a single Phase 2 remediation job from Redis Streams."""
        incident_id = data.get("incident_id")
        if not incident_id:
            logger.error("Received job without incident_id: %s", data)
            self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            return

        logger.info("Processing Phase 2 remediation job for incident %s (msg_id: %s)", incident_id, msg_id)

        # 1. Fetch incident details & severity
        incident_data = get_incident(self.redis, incident_id) or {}
        is_dropped = False
        if self.redis:
            dropped_raw = self.redis.get(f"incident:{incident_id}:dropped")
            if isinstance(dropped_raw, (str, bytes)):
                is_dropped = bool(dropped_raw)

        if incident_data.get("state") == "CLOSED" or is_dropped:
            logger.info("Incident %s is dropped or closed; skipping Phase 2 remediation", incident_id)
            self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            return

        severity = incident_data.get("severity", "P2")

        # 2. Load context, prior attempts, and steering from Redis
        context_raw = self.redis.get(f"incident:{incident_id}:context")
        merged_finding: Dict[str, Any] = {}
        if context_raw:
            try:
                merged_finding = json.loads(context_raw)
            except Exception as exc:
                logger.warning("Failed to decode context for %s: %s", incident_id, exc)

        prior_attempts_raw = self.redis.get(f"incident:{incident_id}:prior_attempts")
        prior_attempts: List[Dict[str, Any]] = []
        if prior_attempts_raw:
            try:
                prior_attempts = json.loads(prior_attempts_raw)
            except Exception as exc:
                logger.warning("Failed to decode prior attempts for %s: %s", incident_id, exc)

        steering_raw = self.redis.get(f"incident:{incident_id}:steering")
        action_id = data.get("action_id")
        approval_status = data.get("approval_status")
        target_proposal = None
        if approval_status == "none":
            approval_status = None
            action_id = None
        elif incident_data.get("state") == "EXECUTING":
            if not action_id:
                action_id = data.get("action_id")
            if not approval_status:
                approval_status = data.get("approval_status")

            if self.redis:
                # 1. Targeted lookup: query specific action record if action_id is known
                if action_id:
                    targeted_key = f"incident:{incident_id}:action:{action_id}"
                    raw_targeted = self.redis.get(targeted_key)
                    if raw_targeted:
                        if isinstance(raw_targeted, (bytes, bytearray)):
                            raw_targeted = raw_targeted.decode("utf-8")
                        try:
                            t_obj = json.loads(raw_targeted)
                            if not approval_status:
                                approval_status = t_obj.get("status")
                            target_proposal = t_obj.get("proposal")
                        except Exception as exc:
                            logger.warning("Failed to decode targeted action for %s: %s", targeted_key, exc)

                # 2. Check approved_actions set in Redis
                if not approval_status or approval_status != "approved":
                    try:
                        approved_set = self.redis.smembers(f"incident:{incident_id}:approved_actions")
                        if approved_set:
                            approved_ids = {m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in approved_set}
                            if action_id and action_id in approved_ids:
                                approval_status = "approved"
                            elif not action_id and approved_ids:
                                action_id = list(approved_ids)[-1]
                                approval_status = "approved"
                                raw_targeted = self.redis.get(f"incident:{incident_id}:action:{action_id}")
                                if raw_targeted:
                                    t_obj = json.loads(raw_targeted.decode("utf-8") if isinstance(raw_targeted, bytes) else raw_targeted)
                                    target_proposal = t_obj.get("proposal")
                    except Exception as set_exc:
                        logger.warning("Failed to query approved_actions set: %s", set_exc)

                # 3. Fallback to top-level incident:{id}:action key if still missing
                if not action_id or not approval_status:
                    raw_act = self.redis.get(f"incident:{incident_id}:action")
                    if raw_act:
                        if isinstance(raw_act, (bytes, bytearray)):
                            raw_act = raw_act.decode("utf-8")
                        try:
                            act_obj = json.loads(raw_act)
                            if not action_id:
                                action_id = act_obj.get("action_id")
                            if not approval_status:
                                approval_status = act_obj.get("status")
                            if not target_proposal:
                                target_proposal = act_obj.get("proposal")
                        except Exception as exc:
                            logger.warning("Failed to decode action object for %s: %s", incident_id, exc)
        elif incident_data.get("state") == "REMEDIATION":
            # Formulation/re-planning mode: do NOT inherit stale approval status from past actions
            approval_status = None
            action_id = None

        retry_count = int(data.get("retry_count", len(prior_attempts)))

        latest_rb_sha = str(read_meta(self.redis, RUNBOOKS_META_KEY).get("sha256") or "")
        if latest_rb_sha and latest_rb_sha != getattr(self, "_runbook_index_sha", ""):
            logger.info(
                "Runbooks index digest changed (%s -> %s); hot-reloading runbook_index.",
                str(getattr(self, "_runbook_index_sha", ""))[:12],
                latest_rb_sha[:12],
            )
            self.runbook_index = load_runbooks_index()
            self._runbook_index_sha = latest_rb_sha

        affected_namespace = (
            incident_data.get("affected_namespace")
            or (merged_finding.get("affected_services", [""])[0] if merged_finding else None)
        )
        incident_runbook_index = (
            load_runbooks_index(namespace=affected_namespace)
            if affected_namespace
            else self.runbook_index
        )

        initial_state: Phase2GraphState = {
            "incident_id": incident_id,
            "action_id": action_id,
            "action_proposal": target_proposal,
            "severity": severity,
            "affected_service": incident_data.get("affected_service", "unknown"),
            "affected_namespace": affected_namespace,
            "merged_finding": merged_finding,
            "runbook_index": incident_runbook_index,
            "prior_attempts": prior_attempts,
            "steering_instructions": steering_raw,
            "approval_status": approval_status,
            "retry_count": retry_count,
        }

        # 3. Execute LangGraph remediation workflow
        audit_token = set_audit_context(incident_id=incident_id, caller="phase2-worker")
        try:
            remediation_graph = self.graph or create_remediation_graph(
                checkpointer=self.checkpointer,
                redis_client=self.redis,
                transition_fn=self._post_transition,
            )
            config = {"configurable": {"thread_id": incident_id}}

            current_state = None
            if hasattr(remediation_graph, "get_state") and getattr(remediation_graph, "checkpointer", None) is not None:
                try:
                    current_state = remediation_graph.get_state(config)
                except Exception:
                    current_state = None

            should_resume_checkpoint = bool(
                current_state
                and getattr(current_state, "next", None)
                and incident_data.get("state") != "REMEDIATION"
                and approval_status in ("approved", "denied")
            )

            if should_resume_checkpoint:
                # Update interrupted state with approval/denial decision to resume execution
                update_fields = {"approval_status": approval_status}
                if steering_raw:
                    update_fields["steering_instructions"] = steering_raw
                if action_id:
                    update_fields["action_id"] = action_id
                if target_proposal:
                    update_fields["action_proposal"] = target_proposal
                remediation_graph.update_state(config, update_fields)
                final_state_raw = await remediation_graph.ainvoke(None, config=config)
            else:
                # Re-plan / steer / retry / initial pass: clear stale checkpoint thread before invoking entry
                cp = getattr(remediation_graph, "checkpointer", None) or self.checkpointer
                if hasattr(cp, "storage") and isinstance(cp.storage, dict):
                    cp.storage.pop(incident_id, None)
                if hasattr(cp, "writes") and isinstance(cp.writes, dict):
                    for wk in [k for k in list(cp.writes.keys()) if isinstance(k, tuple) and k[0] == incident_id]:
                        cp.writes.pop(wk, None)
                final_state_raw = await remediation_graph.ainvoke(initial_state, config=config)

            if isinstance(final_state_raw, dict):
                final_state = final_state_raw
            elif hasattr(remediation_graph, "get_state") and getattr(remediation_graph, "checkpointer", None) is not None:
                try:
                    final_state = remediation_graph.get_state(config).values
                except Exception:
                    final_state = {}
            else:
                final_state = {}

            outcome = final_state.get("outcome")
            logger.info("Incident %s graph execution finished with outcome: %s", incident_id, outcome)

            proposal = final_state.get("action_proposal")
            if proposal:
                # Save latest action proposal to Redis under :proposal
                self.redis.set(f"incident:{incident_id}:proposal", json.dumps(proposal, default=str))

            # 4. Handle graph outcomes and drive state transitions
            if outcome == "action_proposed":
                await self._post_transition(
                    incident_id,
                    "action_proposed",
                    metadata={
                        "proposal": proposal,
                        "action_id": final_state.get("action_id"),
                        "pre_approved": final_state.get("pre_approved", False),
                        "runbook_citations": (proposal or {}).get("runbook_citations", []),
                    },
                )

            elif outcome == "signals_converged":
                await self._post_transition(
                    incident_id,
                    "signals_converged",
                    metadata={"convergence": final_state.get("convergence_result")},
                )

            elif outcome == "signals_not_converged":
                # Record prior attempt with failure signals
                attempt_entry = {
                    "attempt": retry_count + 1,
                    "action": proposal,
                    "outcome": "signals_not_converged",
                    "signals_at_timeout": final_state.get("signals_at_timeout", {}),
                }
                prior_attempts.append(attempt_entry)
                self.redis.set(f"incident:{incident_id}:prior_attempts", json.dumps(prior_attempts, default=str))

                max_retries = self.config.sag.max_retries.get(severity, 3)
                if len(prior_attempts) < max_retries:
                    logger.info(
                        "Incident %s: signals not converged; retry %d/%d queued.",
                        incident_id,
                        len(prior_attempts),
                        max_retries,
                    )
                    await self._post_transition(
                        incident_id,
                        "signals_not_converged",
                        metadata={
                            "prior_attempts_count": len(prior_attempts),
                            "signals_at_timeout": final_state.get("signals_at_timeout", {}),
                            "convergence": final_state.get("convergence_result"),
                        },
                    )
                else:
                    logger.warning("Incident %s: retry limit reached (%d attempts). Escalating.", incident_id, max_retries)
                    await self._post_transition(
                        incident_id,
                        "retry_limit_reached",
                        metadata={"prior_attempts": prior_attempts},
                    )

            elif outcome == "pre_exec_replan":
                logger.info("Incident %s pre-execution check triggered re-plan; transitioning to REMEDIATION", incident_id)
                await self._post_transition(
                    incident_id,
                    "pre_exec_replan",
                    metadata={"reason": "mutation_detected", "delta": final_state.get("mutation_delta")},
                )

            elif outcome in ("low_confidence", "denied", "execution_failed", "escalated"):
                logger.info("Incident %s escalating due to outcome '%s'", incident_id, outcome)
                await self._post_transition(
                    incident_id,
                    "escalate",
                    metadata={"reason": outcome, "error": final_state.get("error")},
                )

        except Exception as e:
            logger.error("Exception during Phase 2 worker execution for %s: %s", incident_id, e, exc_info=True)
            await self._post_transition(
                incident_id,
                "escalate",
                metadata={"error": str(e), "phase": "phase2_remediation"},
            )

        finally:
            reset_audit_context(audit_token)
            try:
                self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            except Exception as e:
                logger.warning("Failed to ACK message %s: %s", msg_id, e)

    async def run(self) -> None:
        """Main worker loop reading from stream:phase2:jobs."""
        self.init_stream_group()
        self.running = True
        logger.info("Phase 2 Remediation Worker service started, listening on %s...", STREAM_NAME)

        while self.running:
            try:
                # Check system emergency brake offloaded from event loop
                raw_pause = await asyncio.to_thread(self.redis.get, "system:status:paused")
                if raw_pause:
                    try:
                        pdata = json.loads(raw_pause) if isinstance(raw_pause, str) else json.loads(raw_pause.decode("utf-8"))
                        if pdata.get("paused"):
                            await asyncio.sleep(2.0)
                            continue
                    except Exception:
                        pass

                entries = await asyncio.to_thread(
                    self.redis.xreadgroup,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    {STREAM_NAME: ">"},
                    count=1,
                    block=2000,
                )

                if not entries:
                    await asyncio.sleep(0.1)
                    continue

                for stream, messages in entries:
                    for msg_id, data in messages:
                        await self.process_job(msg_id, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in Phase 2 worker consumer loop: %s", e)
                if "NOGROUP" in str(e):
                    try:
                        self.init_stream_group()
                    except Exception as ge:
                        logger.warning("Failed to recreate consumer group: %s", ge)
                await asyncio.sleep(1.0)

        logger.info("Phase 2 Remediation Worker service stopped.")

    def stop(self) -> None:
        """Signal worker to stop."""
        self.running = False


async def main():
    worker = Phase2WorkerService()

    def handle_signal(*args):
        logger.info("Received termination signal, stopping worker...")
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception as exc:
            logger.warning("Failed to register signal handler for %s: %s", sig, exc)

    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
