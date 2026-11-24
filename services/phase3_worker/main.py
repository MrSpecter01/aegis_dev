"""Phase 3 Scribe Worker service daemon.

Consumes reporting jobs from Redis Streams (stream:phase3:jobs), executes the
LangGraph Scribe reasoning graph, and coordinates delivery and state closure.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any, Dict, Optional

import redis

from services.phase3_worker.graph import create_scribe_graph
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
logger = logging.getLogger("phase3_worker")

STREAM_NAME = "stream:phase3:jobs"
CONSUMER_GROUP = "phase3-workers"
CONSUMER_NAME = "worker-1"


class Phase3WorkerService:
    """Worker daemon listening to stream:phase3:jobs."""

    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        graph: Optional[Any] = None,
    ):
        self.config = get_config()
        redis_url = os.getenv("REDIS_URL") or self.config.system.redis.url
        self.redis = redis_client or redis.from_url(redis_url, decode_responses=True)
        set_audit_redis_client(self.redis)
        from langgraph.checkpoint.memory import MemorySaver
        self.checkpointer = MemorySaver()
        self.graph = graph or create_scribe_graph(
            redis_client=self.redis,
            checkpointer=self.checkpointer,
        )
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

    async def process_job(self, msg_id: str, data: Dict[str, Any]) -> None:
        """Process a single Phase 3 reporting job from Redis Streams."""
        incident_id = data.get("incident_id")
        if not incident_id:
            logger.error("Received job without incident_id: %s", data)
            self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            return

        logger.info("Received reporting job for incident: %s (msg_id: %s)", incident_id, msg_id)

        initial_state = {
            "incident_id": incident_id,
            "retry_count": 0,
        }

        audit_token = set_audit_context(incident_id=incident_id, caller="phase3-worker")
        try:
            config = {"configurable": {"thread_id": incident_id}}
            final_state = await self.graph.ainvoke(initial_state, config=config)
            logger.info(
                "Scribe completed for %s: is_valid=%s, delivery=%s",
                incident_id,
                final_state.get("is_valid"),
                final_state.get("delivery_result"),
            )
        except Exception as exc:
            logger.exception("Error processing Scribe job for incident %s: %s", incident_id, exc)
        finally:
            reset_audit_context(audit_token)
            self.redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
            logger.info("Acknowledged message %s on %s", msg_id, STREAM_NAME)

    async def run(self) -> None:
        """Main consumer loop."""
        self.init_stream_group()
        self.running = True
        logger.info("Phase 3 Scribe Worker started, listening to %s", STREAM_NAME)

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

                # Read 1 message at a time offloaded from event loop
                messages = await asyncio.to_thread(
                    self.redis.xreadgroup,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    {STREAM_NAME: ">"},
                    count=1,
                    block=2000,
                )
                if not messages:
                    await asyncio.sleep(0.1)
                    continue

                for stream_key, entries in messages:
                    for msg_id, data in entries:
                        await self.process_job(msg_id, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in worker loop: %s", e)
                if "NOGROUP" in str(e):
                    try:
                        self.init_stream_group()
                    except Exception as ge:
                        logger.warning("Failed to recreate consumer group: %s", ge)
                await asyncio.sleep(1)

        logger.info("Phase 3 Scribe Worker stopped.")

    def stop(self) -> None:
        """Signal the worker to stop processing."""
        self.running = False


def main() -> None:
    service = Phase3WorkerService()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_exit(sig, frame):
        logger.info("Shutdown signal received (%s), stopping worker...", sig)
        service.stop()

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    try:
        loop.run_until_complete(service.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
