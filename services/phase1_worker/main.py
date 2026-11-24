"""Phase 1 Worker main service entry point.

Consumes investigation jobs from Redis Streams (stream:phase1:jobs),
runs the Phase1Flow CrewAI Flow, persists MergedFinding to Redis,
and advances the state machine via API callbacks.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any, Dict, Optional

import httpx
import redis

from services.api_server.incident_store import get_incident
from services.phase1_worker.flow import Phase1Flow
from shared.audit.tracker import (
    reset_audit_context,
    set_audit_context,
    set_audit_redis_client,
)
from shared.config.loader import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("phase1_worker")

STREAM_NAME = "stream:phase1:jobs"
CONSUMER_GROUP = "phase1-workers"
CONSUMER_NAME = "worker-1"


class Phase1WorkerService:
    """Worker daemon listening to stream:phase1:jobs."""

    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
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
            "actor": "phase1_worker",
            "metadata": metadata or {},
        }
        headers = {
            "Content-Type": "application/json",
            "X-Aegis-Service": "phase1-worker",
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
        """Process a single Phase 1 job from Redis Streams."""
        incident_id = data.get("incident_id")
        if not incident_id:
            logger.error("Received job without incident_id: %s", data)
            self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            return

        logger.info("Processing Phase 1 job for incident %s (msg_id: %s)", incident_id, msg_id)

        # 1. Fetch incident details if not in message
        incident_data = get_incident(self.redis, incident_id) or {}
        is_dropped = False
        if self.redis:
            dropped_raw = self.redis.get(f"incident:{incident_id}:dropped")
            if isinstance(dropped_raw, (str, bytes)):
                is_dropped = bool(dropped_raw)

        if incident_data.get("state") == "CLOSED" or is_dropped:
            logger.info("Incident %s is dropped or closed; skipping Phase 1 processing", incident_id)
            self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            return

        affected_service = data.get("affected_service") or incident_data.get("affected_service", "unknown")
        affected_namespace = data.get("affected_namespace") or incident_data.get("affected_namespace", "default")

        # Parse alert metadata and topology subgraph
        alert_metadata = data.get("alert_metadata")
        if isinstance(alert_metadata, str):
            try:
                alert_metadata = json.loads(alert_metadata)
            except Exception:
                alert_metadata = {}

        if not alert_metadata and self.redis:
            raw_group = self.redis.get(f"incident:{incident_id}:alert_group")
            if raw_group:
                try:
                    if isinstance(raw_group, bytes):
                        raw_group = raw_group.decode("utf-8")
                    if isinstance(raw_group, str):
                        alert_metadata = json.loads(raw_group)
                    elif isinstance(raw_group, dict):
                        alert_metadata = raw_group
                except Exception as e:
                    logger.warning("Failed to parse alert_group from Redis for incident %s: %s", incident_id, e)

        if not isinstance(alert_metadata, dict):
            alert_metadata = {}

        if "severity" not in alert_metadata and incident_data.get("severity"):
            alert_metadata["severity"] = incident_data["severity"]

        topology_subgraph = data.get("topology_subgraph")
        if isinstance(topology_subgraph, str):
            try:
                topology_subgraph = json.loads(topology_subgraph)
            except Exception:
                topology_subgraph = {}

        if not topology_subgraph and self.redis and affected_service and affected_service != "unknown":
            try:
                from services.api_server.topology.store import TopologyStore
                from services.api_server.topology.query import get_subgraph
                store = TopologyStore(self.redis)
                subgraph = get_subgraph(store, affected_service)
                topology_subgraph = subgraph.model_dump(mode="json")
                if getattr(subgraph, "status", None) == "MISSING_DATA" or getattr(getattr(subgraph, "target_node", None), "stub", False):
                    logger.warning(
                        "[TOPOLOGY WARNING] Query for topology subgraph for service '%s' returned missing data: status=MISSING_DATA",
                        affected_service,
                    )
            except Exception as e:
                logger.debug("Failed to resolve topology_subgraph from Redis for %s: %s", affected_service, e)

        if not isinstance(topology_subgraph, dict):
            topology_subgraph = {}

        # Short-circuit repeated chronic recurrences with prior recent escalations to protect API limits
        if alert_metadata.get("chronic_recurrence"):
            prior_recs = alert_metadata.get("recent_recurrences") or []
            threshold = max(2, int(getattr(self.config.system.ingestion, "recurrence_threshold", 3)))
            ended_recs = [r for r in prior_recs if r.get("state") in ("ESCALATED", "CLOSED")]
            has_escalated = any(r.get("state") == "ESCALATED" for r in prior_recs)
            if has_escalated and len(ended_recs) >= (threshold - 1):
                rec_count = alert_metadata.get("recurrence_count", len(prior_recs) + 1)
                logger.warning(
                    "Incident %s hit chronic recurrence threshold (%s in window with prior escalation); fast-escalating.",
                    incident_id,
                    rec_count,
                )
                try:
                    from shared.models.findings import MergedFinding
                    from datetime import datetime, timezone
                    summary_hyp = (
                        f"Chronic recurrence detected ({rec_count} occurrences within recurrence window) "
                        f"for service '{affected_service}' following prior escalation; escalating immediately."
                    )
                    fast_merged = MergedFinding(
                        incident_id=incident_id,
                        primary_hypothesis=summary_hyp,
                        merged_confidence=0.9,
                        findings_by_agent={},
                        ranked_findings=[],
                        all_evidence=[],
                        verbatim_signals={"recent_recurrences": prior_recs, "recurrence_count": rec_count},
                        infra_flags={},
                        corroboration_score=0.0,
                        merged_at=datetime.now(timezone.utc),
                    )
                    if self.redis:
                        self.redis.set(f"incident:{incident_id}:context", fast_merged.model_dump_json())
                    await self._post_transition(incident_id, "triage_complete")
                    await self._post_transition(
                        incident_id,
                        "merge_complete",
                        metadata={"primary_hypothesis": summary_hyp, "merged_confidence": 0.9, "corroboration_score": 0.0},
                    )
                    await self._post_transition(
                        incident_id,
                        "escalate",
                        metadata={
                            "reason": "chronic_recurrence_threshold_exceeded",
                            "recurrence_count": rec_count,
                            "recent_recurrences": prior_recs,
                        },
                    )
                finally:
                    try:
                        self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
                    except Exception as exc:
                        logger.warning("Failed to ACK message %s: %s", msg_id, exc)
                return

        # 2. Run Phase1Flow
        audit_token = set_audit_context(incident_id=incident_id, caller="phase1-worker")
        try:
            flow = Phase1Flow(
                incident_id=incident_id,
                affected_namespace=affected_namespace,
                affected_service=affected_service,
                alert_metadata=alert_metadata or {},
                topology_subgraph=topology_subgraph or {},
                redis_client=self.redis,
            )

            merged_finding = await flow.run()

            # 3. Transition TRIAGE -> MERGING
            await self._post_transition(incident_id, "triage_complete")

            # 4. Transition MERGING -> REMEDIATION
            await self._post_transition(
                incident_id,
                "merge_complete",
                metadata={
                    "primary_hypothesis": merged_finding.primary_hypothesis,
                    "merged_confidence": merged_finding.merged_confidence,
                    "corroboration_score": merged_finding.corroboration_score,
                },
            )

        except Exception as e:
            logger.error("Exception during Phase 1 investigation for %s: %s", incident_id, e, exc_info=True)
            await self._post_transition(
                incident_id,
                "escalate",
                metadata={"error": str(e), "phase": "phase1_investigation"},
            )

        finally:
            reset_audit_context(audit_token)
            # Always ACK message
            try:
                self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            except Exception as e:
                logger.warning("Failed to ACK message %s: %s", msg_id, e)

    async def run(self) -> None:
        """Main worker loop reading from stream:phase1:jobs."""
        self.init_stream_group()
        self.running = True
        logger.info("Phase 1 Worker service started, listening on %s...", STREAM_NAME)

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

                # Read from Redis stream offloaded from event loop
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
                logger.error("Error in worker consumer loop: %s", e)
                if "NOGROUP" in str(e):
                    try:
                        self.init_stream_group()
                    except Exception as ge:
                        logger.warning("Failed to recreate consumer group: %s", ge)
                await asyncio.sleep(1.0)

        logger.info("Phase 1 Worker service stopped.")

    def stop(self) -> None:
        """Signal worker to stop."""
        self.running = False


async def main():
    worker = Phase1WorkerService()

    def handle_signal(*args):
        logger.info("Received termination signal, stopping worker...")
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to register signal handler for %s: %s", sig, exc)

    await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
