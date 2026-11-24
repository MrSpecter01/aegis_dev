import json
import time
from datetime import datetime, timezone
import pytest
import redis

from shared.config.schemas import IngestionConfig
from shared.models.alerts import Alert, AlertGroup
from shared.models.enums import Severity, IncidentState
from shared.models.topology import TopologyNode, TopologyEdge
from services.api_server.topology.store import TopologyStore
from services.api_server.ingestion.correlator import AlertCorrelator


class CorrelatorFakeRedis(redis.Redis):
    def __init__(self):
        self.store = {}
        self.sets = {}
        self.lists = {}
        self.expirations = {}

    def set(self, key, value, *args, **kwargs):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def rpush(self, key, value):
        if key not in self.lists:
            self.lists[key] = []
        self.lists[key].append(value)

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start:end+1]

    def incr(self, key):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = val
        return val

    def expire(self, key, seconds):
        self.expirations[key] = seconds

    def sadd(self, key, member):
        if key not in self.sets:
            self.sets[key] = set()
        self.sets[key].add(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, member):
        if key in self.sets:
            self.sets[key].discard(member)

    def scan_iter(self, match):
        prefix = match.replace("*", "")
        for k in list(self.store.keys()):
            if k.startswith(prefix):
                yield k

    def keys(self, pattern="*"):
        import fnmatch
        all_keys = set(self.store.keys()) | set(self.lists.keys()) | set(self.sets.keys())
        return [k for k in all_keys if fnmatch.fnmatch(k, pattern)]

    def xadd(self, stream, fields):
        pass

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                count += 1
            self.expirations.pop(k, None)
        return count


@pytest.fixture
def fake_redis():
    return CorrelatorFakeRedis()


@pytest.fixture
def topology_store(fake_redis, tmp_path):
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "snap.json"))
    nodes = [
        TopologyNode(
            id="frontend.boutique",
            name="frontend",
            namespace="boutique",
            ports=[8080],
            dependencies=["cartservice.boutique"],
        ),
        TopologyNode(
            id="cartservice.boutique",
            name="cartservice",
            namespace="boutique",
            ports=[7070],
            dependencies=["redis-cart.boutique"],
        ),
        TopologyNode(
            id="redis-cart.boutique",
            name="redis-cart",
            namespace="boutique",
            ports=[6379],
            dependencies=[],
        ),
    ]
    edges = [
        TopologyEdge(from_node="frontend.boutique", to_node="cartservice.boutique", port=7070),
        TopologyEdge(from_node="cartservice.boutique", to_node="redis-cart.boutique", port=6379),
    ]
    store.save(nodes, edges)
    return store


@pytest.fixture
def correlator(fake_redis, topology_store):
    config = IngestionConfig(
        correlation_window_seconds=120,
        flap_window_seconds=60,
        flap_threshold=5,
        severity_map={"critical": "P1", "warning": "P2", "info": "P3"},
    )
    return AlertCorrelator(fake_redis, config, topology_store)


@pytest.mark.asyncio
async def test_two_adjacent_alerts_grouped_into_one_incident(correlator):
    # Alert 1: frontend
    payload1 = {
        "groupKey": "frontend-alert",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighHTTP500Errors",
                    "service": "frontend",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-fe-1",
            }
        ],
    }
    inc_id1 = await correlator.process(payload1)
    assert inc_id1 is not None
    assert inc_id1.startswith("inc-")

    # Alert 2: cartservice (adjacent to frontend) fired within correlation window
    payload2 = {
        "groupKey": "cartservice-alert",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighLatency",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-cart-1",
            }
        ],
    }
    inc_id2 = await correlator.process(payload2)
    
    # Must group into the same incident!
    assert inc_id2 == inc_id1

    # Check alert group has both
    grp = correlator.get_alert_group(inc_id1)
    assert grp is not None
    assert grp.primary_alert.service == "frontend"
    assert len(grp.related_alerts) == 1
    assert grp.related_alerts[0].service == "cartservice"
    # Severity escalated from warning (P2) to critical (P1)
    assert grp.severity == Severity.P1


@pytest.mark.asyncio
async def test_duplicate_fingerprint_merges_not_creates(correlator):
    payload = {
        "groupKey": "dup-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighCPU",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-dup-1",
            }
        ],
    }
    inc1 = await correlator.process(payload)
    assert inc1 is not None

    # Arrives again within window
    inc2 = await correlator.process(payload)
    assert inc2 == inc1

    grp = correlator.get_alert_group(inc1)
    assert len(grp.related_alerts) == 0
    assert "fp-dup-1" in grp.fingerprints


@pytest.mark.asyncio
async def test_fingerprint_ttl_refreshed_on_dedupe(correlator, fake_redis):
    payload = {
        "groupKey": "ttl-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "MemLeak",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-ttl-1",
            }
        ],
    }
    inc1 = await correlator.process(payload)
    assert fake_redis.expirations.get("incident:fingerprints:fp-ttl-1") == correlator.config.fingerprint_ttl_seconds

    # Simulate some time passed / change expiration
    fake_redis.expirations["incident:fingerprints:fp-ttl-1"] = 100

    # 2nd alert arrives -> TTL should be refreshed
    inc2 = await correlator.process(payload)
    assert inc2 == inc1
    assert fake_redis.expirations.get("incident:fingerprints:fp-ttl-1") == correlator.config.fingerprint_ttl_seconds


@pytest.mark.asyncio
async def test_dedupe_after_close_creates_new_incident(correlator, fake_redis):
    from services.api_server.incident_store import update_incident_state
    payload = {
        "groupKey": "close-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "OOMKilled",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-close-1",
            }
        ],
    }
    inc1 = await correlator.process(payload)
    assert inc1 is not None

    # Close the incident via update_incident_state
    update_incident_state(fake_redis, inc1, "CLOSED")

    # Next alert with same fingerprint should create a new incident
    inc2 = await correlator.process(payload)
    assert inc2 is not None
    assert inc2 != inc1



@pytest.mark.asyncio
async def test_no_topology_match_creates_own_incident_with_warning(correlator):
    payload = {
        "groupKey": "unknown-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "DiskPressure",
                    "service": "unknown-payment",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-unknown-1",
            }
        ],
    }
    inc_id = await correlator.process(payload)
    assert inc_id is not None

    grp = correlator.get_alert_group(inc_id)
    assert grp is not None
    assert grp.correlation_warning is True


@pytest.mark.asyncio
async def test_severity_label_mapped_to_p2(correlator):
    payload = {
        "groupKey": "sev-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "WarningAlert",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-sev-warning",
            }
        ],
    }
    inc_id = await correlator.process(payload)
    grp = correlator.get_alert_group(inc_id)
    assert grp.severity == Severity.P2


@pytest.mark.asyncio
async def test_missing_severity_defaults_to_p3(correlator):
    payload = {
        "groupKey": "missing-sev-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "NoSeverityAlert",
                    "service": "cartservice",
                    "namespace": "boutique",
                },
                "fingerprint": "fp-missing-sev",
            }
        ],
    }
    inc_id = await correlator.process(payload)
    grp = correlator.get_alert_group(inc_id)
    assert grp.severity == Severity.P3


@pytest.mark.asyncio
async def test_flap_detection_sets_flag_and_suppresses(correlator):
    payload = {
        "groupKey": "flap-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "FlappingAlert",
                    "service": "redis-cart",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-flap-1",
            }
        ],
    }
    # Initial creation (count=1)
    inc_id = await correlator.process(payload)
    assert inc_id is not None

    # Trigger counts up to threshold (5)
    for _ in range(4):
        await correlator.process(payload)

    # 6th trigger exceeds threshold 5 -> flap detected!
    res = await correlator.process(payload)
    assert res is None  # Suppressed!

    grp = correlator.get_alert_group(inc_id)
    assert grp.flapping is True


@pytest.mark.asyncio
async def test_flap_counter_resets_after_window(correlator, fake_redis):
    # If the key expired in redis, incr starts over at 1
    fp = "fp-reset-test"
    fake_redis.store[f"flap:count:{fp}"] = 0
    assert not correlator._check_flapping(fp)


@pytest.mark.asyncio
async def test_resolved_only_alert_does_not_create_incident(correlator):
    payload = {
        "groupKey": "resolved-only",
        "alerts": [
            {
                "status": "resolved",
                "labels": {
                    "alertname": "OtelElevatedSpanErrorRate",
                    "service": "frontend",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-resolved-only-1",
            }
        ],
    }
    res = await correlator.process(payload)
    assert res is None


@pytest.mark.asyncio
async def test_same_service_different_alert_correlates_when_open(correlator):
    payload1 = {
        "groupKey": "svc-err",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "OtelElevatedSpanErrorRate",
                    "service": "frontend",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-same-svc-err",
            }
        ],
    }
    inc1 = await correlator.process(payload1)
    assert inc1 is not None

    payload2 = {
        "groupKey": "svc-lat",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "OtelHighSpanLatency",
                    "service": "frontend",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-same-svc-lat",
            }
        ],
    }
    inc2 = await correlator.process(payload2)
    assert inc2 == inc1


@pytest.mark.asyncio
async def test_correlator_recovers_open_incident_missing_from_active_set(correlator, fake_redis):
    """If active:namespace:{ns} lost an incident id, correlator recovers it via incident:*:state scan."""
    payload1 = {
        "groupKey": "cart-down",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubeDeploymentReplicasMismatch",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-miss-1",
            }
        ],
    }
    inc1 = await correlator.process(payload1)
    assert inc1 is not None

    # Simulate stale/incomplete active set index
    fake_redis.srem("active:namespace:boutique", inc1)
    assert inc1 not in fake_redis.smembers("active:namespace:boutique")

    payload2 = {
        "groupKey": "fe-err",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "OtelElevatedSpanErrorRate",
                    "service": "frontend",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-miss-2",
            }
        ],
    }
    inc2 = await correlator.process(payload2)
    assert inc2 == inc1


@pytest.mark.asyncio
async def test_correlator_groups_two_hop_cascade_within_window_and_respects_fixed_clock(correlator, fake_redis):
    """2-hop cascade groups within fixed window from creation without resetting created_at."""
    payload1 = {
        "groupKey": "redis-down",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ContainerDown",
                    "service": "redis-cart",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-2hop-redis",
            }
        ],
    }
    inc1 = await correlator.process(payload1)
    assert inc1 is not None

    # Within fixed 120s window (80s old), missing from active set, still recovers & merges 2-hop frontend
    group_key = f"incident:{inc1}:alert_group"
    group_data = json.loads(fake_redis.store[group_key])
    orig_created_at = group_data["created_at"]
    within_ts = datetime.fromtimestamp(time.time() - 80, tz=timezone.utc).isoformat()
    group_data["created_at"] = within_ts
    fake_redis.store[group_key] = json.dumps(group_data)
    fake_redis.srem("active:namespace:boutique", inc1)
    state_data = json.loads(fake_redis.store[f"incident:{inc1}:state"])
    state_data["state"] = "REMEDIATION"
    fake_redis.store[f"incident:{inc1}:state"] = json.dumps(state_data)

    payload2 = {
        "groupKey": "fe-cascade",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighHTTP500",
                    "service": "frontend",
                    "namespace": "boutique",
                    "severity": "warning",
                },
                "fingerprint": "fp-2hop-fe",
            }
        ],
    }
    inc2 = await correlator.process(payload2)
    assert inc2 == inc1
    # Merging must not reset created_at
    assert correlator.get_alert_group(inc1).created_at == datetime.fromisoformat(within_ts)
    assert orig_created_at != within_ts


@pytest.mark.asyncio
async def test_correlator_chronic_recurrence_window_pruning(fake_redis, topology_store):
    """Stale recurrences older than recurrence_window_seconds are pruned; >=3 within window flags chronic."""
    cfg = IngestionConfig(
        correlation_window_seconds=180,
        recurrence_window_seconds=900,
        recurrence_threshold=3,
        topology_max_hops=2,
        flap_window_seconds=60,
        flap_threshold=10,
    )
    corr = AlertCorrelator(fake_redis, cfg, topology_store)

    now = time.time()
    rec_key = "recurrence:history:boutique:cartservice:CartDown"
    # Pre-seed 2 stale (> 900s ago) and 2 fresh (within 900s) closed incidents
    fake_redis.store[rec_key] = json.dumps(
        [
            {"incident_id": "inc-stale-1", "timestamp": now - 1500, "outcome": "ESCALATED"},
            {"incident_id": "inc-stale-2", "timestamp": now - 1200, "outcome": "ESCALATED"},
            {"incident_id": "inc-recent-1", "timestamp": now - 300, "outcome": "ESCALATED"},
            {"incident_id": "inc-recent-2", "timestamp": now - 120, "outcome": "ESCALATED"},
        ]
    )

    payload = {
        "groupKey": "cart-chronic",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "CartDown",
                    "service": "cartservice",
                    "namespace": "boutique",
                    "severity": "critical",
                },
                "fingerprint": "fp-chronic-1",
            }
        ],
    }
    inc_id = await corr.process(payload)
    assert inc_id is not None

    grp = corr.get_alert_group(inc_id)
    assert grp is not None
    assert grp.chronic_recurrence is True
    # Only the 2 recent entries + current incident remain (stale pruned out)
    assert grp.recurrence_count == 3
    kept_ids = [r["incident_id"] for r in grp.recent_recurrences]
    assert "inc-stale-1" not in kept_ids
    assert "inc-stale-2" not in kept_ids
    assert "inc-recent-1" in kept_ids
    assert "inc-recent-2" in kept_ids
    stored_ids = [r["incident_id"] for r in json.loads(fake_redis.store[rec_key])]
    assert inc_id in stored_ids
    assert "inc-stale-1" not in stored_ids


@pytest.mark.asyncio
async def test_correlator_no_transitive_hop_creep(fake_redis, tmp_path):
    """Ensure 2-hop radius stays anchored on origin service and never creeps 3+ hops via merged alerts."""
    store = TopologyStore(fake_redis, snapshot_path=str(tmp_path / "chain_snap.json"))
    nodes = [
        TopologyNode(id="svc-a.boutique", name="svc-a", namespace="boutique", dependencies=["svc-b.boutique"]),
        TopologyNode(id="svc-b.boutique", name="svc-b", namespace="boutique", dependencies=["svc-c.boutique"]),
        TopologyNode(id="svc-c.boutique", name="svc-c", namespace="boutique", dependencies=["svc-d.boutique"]),
        TopologyNode(id="svc-d.boutique", name="svc-d", namespace="boutique", dependencies=[]),
    ]
    edges = [
        TopologyEdge(from_node="svc-a.boutique", to_node="svc-b.boutique", port=80),
        TopologyEdge(from_node="svc-b.boutique", to_node="svc-c.boutique", port=80),
        TopologyEdge(from_node="svc-c.boutique", to_node="svc-d.boutique", port=80),
    ]
    store.save(nodes, edges)
    corr = AlertCorrelator(fake_redis, IngestionConfig(correlation_window_seconds=180, topology_max_hops=2), store)

    inc_a = await corr.process(
        {
            "groupKey": "a-fail",
            "alerts": [{"status": "firing", "labels": {"alertname": "ErrA", "service": "svc-a", "namespace": "boutique", "severity": "critical"}, "fingerprint": "fp-a"}],
        }
    )
    # svc-c is 2 hops from origin svc-a -> correlates into inc_a
    inc_c = await corr.process(
        {
            "groupKey": "c-fail",
            "alerts": [{"status": "firing", "labels": {"alertname": "ErrC", "service": "svc-c", "namespace": "boutique", "severity": "warning"}, "fingerprint": "fp-c"}],
        }
    )
    assert inc_c == inc_a

    # svc-d is 1 hop from svc-c, but 3 hops from origin svc-a -> must create a NEW incident
    inc_d = await corr.process(
        {
            "groupKey": "d-fail",
            "alerts": [{"status": "firing", "labels": {"alertname": "ErrD", "service": "svc-d", "namespace": "boutique", "severity": "warning"}, "fingerprint": "fp-d"}],
        }
    )
    assert inc_d is not None
    assert inc_d != inc_a


@pytest.mark.asyncio
async def test_correlator_escalated_incident_not_swallowed_and_re_alerts_create_new(correlator, fake_redis):
    """Hanging ESCALATED incidents must not swallow or merge-and-ignore subsequent alerts."""
    from services.api_server.incident_store import update_incident_state

    payload = {
        "groupKey": "esc-test",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighHTTP500", "service": "frontend", "namespace": "boutique", "severity": "critical"},
                "fingerprint": "fp-esc-same",
            }
        ],
    }
    inc1 = await correlator.process(payload)
    update_incident_state(fake_redis, inc1, "ESCALATED")

    inc2 = await correlator.process(payload)
    assert inc2 is not None
    assert inc2 != inc1


@pytest.mark.asyncio
async def test_correlator_awaiting_approval_same_alert_vs_mutation_past_window(correlator, fake_redis):
    """Past 180s in AWAITING_APPROVAL: identical alert merges safely; mutated alert triggers steer re-plan."""
    from services.api_server.incident_store import get_incident, update_incident_state

    inc1 = await correlator.process(
        {
            "groupKey": "wait-cart",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighErrorRate", "service": "cartservice", "namespace": "boutique", "severity": "warning"},
                    "fingerprint": "fp-wait-1",
                }
            ],
        }
    )
    update_incident_state(fake_redis, inc1, "AWAITING_APPROVAL")
    group_key = f"incident:{inc1}:alert_group"
    grp_data = json.loads(fake_redis.store[group_key])
    grp_data["created_at"] = datetime.fromtimestamp(time.time() - 300, tz=timezone.utc).isoformat()
    fake_redis.store[group_key] = json.dumps(grp_data)

    # Same alert with new fingerprint while awaiting approval past 180s -> merges, stays AWAITING_APPROVAL
    inc_same = await correlator.process(
        {
            "groupKey": "wait-cart-2",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighErrorRate", "service": "cartservice", "namespace": "boutique", "severity": "warning"},
                    "fingerprint": "fp-wait-2",
                }
            ],
        }
    )
    assert inc_same == inc1
    assert get_incident(fake_redis, inc1)["state"] == "AWAITING_APPROVAL"

    # Mutated fault alert on same origin service while awaiting approval -> writes mutation_delta and triggers steer -> REMEDIATION
    inc_mut = await correlator.process(
        {
            "groupKey": "wait-cart-mut",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "OOMKilled", "service": "cartservice", "namespace": "boutique", "severity": "critical"},
                    "fingerprint": "fp-wait-mut",
                }
            ],
        }
    )
    assert inc_mut == inc1
    assert "OOMKilled" in fake_redis.store.get(f"incident:{inc1}:mutation_delta", "")
    assert get_incident(fake_redis, inc1)["state"] == "REMEDIATION"


@pytest.mark.asyncio
async def test_alert_multi_pod_resolution_and_affected_pods(correlator):
    """Multiple pods of a Deployment or StatefulSet normalize to the owning service and retain pod names."""
    a_proxy = Alert(labels={"alertname": "High500", "pod": "frontend-proxy-7d8f9c6b5-k2m9z", "namespace": "boutique"})
    assert a_proxy.service == "frontend-proxy"

    a_sts = Alert(labels={"alertname": "RedisDown", "pod": "redis-cart-0", "namespace": "boutique"})
    assert a_sts.service == "redis-cart"

    inc1 = await correlator.process(
        {
            "groupKey": "multi-pod",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "PodCrashLoop", "pod": "cartservice-85885db58b-fw6sx", "namespace": "boutique", "severity": "critical"},
                    "fingerprint": "fp-pod-1",
                },
                {
                    "status": "firing",
                    "labels": {"alertname": "PodCrashLoop", "pod": "cartservice-85885db58b-m4p8q", "namespace": "boutique", "severity": "critical"},
                    "fingerprint": "fp-pod-2",
                },
            ],
        }
    )
    grp = correlator.get_alert_group(inc1)
    assert grp.affected_service == "cartservice"
    assert grp.affected_pods == ["cartservice-85885db58b-fw6sx", "cartservice-85885db58b-m4p8q"]

