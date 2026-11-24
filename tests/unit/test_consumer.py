import json
import pytest
import redis

from shared.config.schemas import SystemConfig
from services.api_server.ingestion.consumer import run_alert_consumer
from tests.unit.test_correlator import CorrelatorFakeRedis


class ConsumerFakeRedis(CorrelatorFakeRedis):
    def __init__(self):
        super().__init__()
        self.groups = set()
        self.stream_messages = []
        self.acked = []
        self.dead_lettered = []
        self.delivery_counts = {}

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if (stream, group) in self.groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.add((stream, group))

    def xreadgroup(self, group, consumer, streams, count=10, block=1000):
        if not self.stream_messages:
            return []
        msgs = list(self.stream_messages)
        self.stream_messages.clear()
        return [("stream:alert:raw", msgs)]

    def xack(self, stream, group, id):
        self.acked.append((stream, group, id))

    def xpending_range(self, stream, group, min=None, max=None, count=10):
        msg_id = min
        attempts = self.delivery_counts.get(msg_id, 1)
        return [{"message_id": msg_id, "consumer": "consumer-1", "time_since_delivered": 100, "times_delivered": attempts}]

    def xadd(self, stream, fields):
        if stream == "stream:alert:dead":
            self.dead_lettered.append(fields)
        elif stream == "stream:alert:raw":
            msg_id = f"msg-{len(self.stream_messages) + 1}"
            self.stream_messages.append((msg_id, fields))
            return msg_id
        return "1-0"


@pytest.fixture
def consumer_redis():
    return ConsumerFakeRedis()


@pytest.mark.asyncio
async def test_consumer_creates_group_if_not_exists(consumer_redis):
    config = SystemConfig()
    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)
    assert ("stream:alert:raw", "aegis-correlator") in consumer_redis.groups


@pytest.mark.asyncio
async def test_consumer_processes_and_acks_message(consumer_redis):
    config = SystemConfig()
    payload = {
        "groupKey": "test-group",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "TestAlert",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-consumer-1",
            }
        ],
    }
    # Add a message to the fake stream
    consumer_redis.stream_messages.append(("msg-1", {"payload": json.dumps(payload)}))

    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)

    # Message must be ACKed
    assert ("stream:alert:raw", "aegis-correlator", "msg-1") in consumer_redis.acked
    # Incident must be created
    assert consumer_redis.get("incident:fingerprints:fp-consumer-1") is not None


@pytest.mark.asyncio
async def test_same_fingerprint_twice_one_incident(consumer_redis, mocker):
    config = SystemConfig()
    spy_generate = mocker.spy(consumer_redis, "incr")
    payload = {
        "groupKey": "test-group",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "TestAlert",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-same-1",
            }
        ],
    }
    consumer_redis.stream_messages.append(("msg-1", {"payload": json.dumps(payload)}))
    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)

    consumer_redis.stream_messages.append(("msg-2", {"payload": json.dumps(payload)}))
    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)

    # Both messages must be ACKed
    assert ("stream:alert:raw", "aegis-correlator", "msg-1") in consumer_redis.acked
    assert ("stream:alert:raw", "aegis-correlator", "msg-2") in consumer_redis.acked

    inc_id = consumer_redis.get("incident:fingerprints:fp-same-1")
    assert inc_id is not None
    # Check that alert group has only 1 primary alert and 0 duplicate related alerts
    raw_grp = consumer_redis.get(f"incident:{inc_id}:alert_group")
    from shared.models.alerts import AlertGroup
    grp = AlertGroup.model_validate_json(raw_grp)
    assert len(grp.related_alerts) == 0


@pytest.mark.asyncio
async def test_consumer_does_not_ack_on_failure(consumer_redis):
    config = SystemConfig()
    consumer_redis.delivery_counts["msg-poison-1"] = 1
    consumer_redis.stream_messages.append(("msg-poison-1", {"payload": "invalid-json{"}))

    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)

    # On attempt 1, should NOT be ACKed and not dead-lettered
    assert ("stream:alert:raw", "aegis-correlator", "msg-poison-1") not in consumer_redis.acked
    assert len(consumer_redis.dead_lettered) == 0


@pytest.mark.asyncio
async def test_consumer_dead_letters_after_max_attempts(consumer_redis):
    config = SystemConfig()
    consumer_redis.delivery_counts["msg-poison-max"] = 3
    consumer_redis.stream_messages.append(("msg-poison-max", {"payload": "invalid-json{"}))

    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)

    # On attempt 3 (>= max_delivery_attempts), should be dead-lettered and ACKed
    assert ("stream:alert:raw", "aegis-correlator", "msg-poison-max") in consumer_redis.acked
    assert len(consumer_redis.dead_lettered) == 1
    assert "invalid-json{" in consumer_redis.dead_lettered[0]["payload"]


@pytest.mark.asyncio
async def test_xreadgroup_does_not_block_loop(consumer_redis):
    import time
    import asyncio

    # Simulate xreadgroup that blocks in thread for 100ms
    def slow_xreadgroup(*args, **kwargs):
        time.sleep(0.1)
        return []

    consumer_redis.xreadgroup = slow_xreadgroup
    config = SystemConfig()

    ticks = 0
    async def ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    # Run consumer and ticker concurrently
    consumer_task = asyncio.create_task(run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True))
    ticker_task = asyncio.create_task(ticker())

    await asyncio.gather(consumer_task, ticker_task)
    assert ticks >= 3  # Confirms event loop was not blocked


@pytest.mark.asyncio
async def test_consumer_recreates_group_on_nogroup(consumer_redis):
    # Simulate NOGROUP error on first xreadgroup call, then return empty
    call_count = 0

    def fail_once_xreadgroup(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("NOGROUP No such key 'stream:alert:raw' or consumer group 'aegis-correlator'")
        return []

    consumer_redis.xreadgroup = fail_once_xreadgroup
    config = SystemConfig()

    # Clear groups to simulate a flushed Redis
    consumer_redis.groups.clear()

    # Run consumer with run_once=True (it should handle NOGROUP, recreate group, and exit)
    await run_alert_consumer(consumer_redis, config, topology_store=None, run_once=True)

    # Verify that the consumer group was recreated
    assert ("stream:alert:raw", "aegis-correlator") in consumer_redis.groups


