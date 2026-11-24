import pytest
from datetime import datetime, timezone
import json

from shared.models.alerts import AlertGroup, Alert
from shared.models.enums import Severity, IncidentState
from services.api_server.incident_store import (
    generate_incident_id,
    create_incident,
    get_incident,
    list_incidents,
    set_incident_ttl
)

@pytest.fixture
def mock_redis(mocker):
    client = mocker.Mock()
    # Fake isinstance check for redis.Redis
    import redis
    client.__class__ = redis.Redis
    client.incr.return_value = 1
    return client

def test_generate_incident_id_format(mock_redis):
    inc_id = generate_incident_id(mock_redis)
    assert inc_id.startswith("inc-")
    assert inc_id.endswith("-0001")
    assert len(inc_id.split("-")) == 3

def test_generate_incident_id_increments(mock_redis):
    mock_redis.incr.side_effect = [1, 2]
    id1 = generate_incident_id(mock_redis)
    id2 = generate_incident_id(mock_redis)
    assert id1.endswith("-0001")
    assert id2.endswith("-0002")

def test_create_incident_writes_redis(mock_redis):
    alert = Alert(labels={"severity": "P2", "service": "cartservice", "namespace": "default"})
    group = AlertGroup(
        group_id="g1",
        primary_alert=alert,
        severity=Severity.P2,
        affected_service="cartservice",
        affected_namespace="default"
    )
    data = create_incident(mock_redis, "inc-123", group)
    assert data["state"] == IncidentState.NEW.value
    assert data["severity"] == "P2"
    assert data["affected_service"] == "cartservice"
    mock_redis.set.assert_called_once()
    args = mock_redis.set.call_args[0]
    assert args[0] == "incident:inc-123:state"
    assert json.loads(args[1])["state"] == "NEW"

def test_get_incident_returns_none_for_missing(mock_redis):
    mock_redis.get.return_value = None
    assert get_incident(mock_redis, "inc-123") is None

def test_list_incidents_filters_by_state(mock_redis):
    mock_redis.scan_iter.return_value = ["incident:inc-1:state", "incident:inc-2:state"]
    mock_redis.get.side_effect = [
        json.dumps({"incident_id": "inc-1", "state": "NEW", "severity": "P2"}),
        json.dumps({"incident_id": "inc-2", "state": "TRIAGE", "severity": "P2"})
    ]
    
    all_incs = list_incidents(mock_redis)
    assert len(all_incs) == 2
    
    mock_redis.scan_iter.return_value = ["incident:inc-1:state", "incident:inc-2:state"]
    mock_redis.get.side_effect = [
        json.dumps({"incident_id": "inc-1", "state": "NEW", "severity": "P2"}),
        json.dumps({"incident_id": "inc-2", "state": "TRIAGE", "severity": "P2"})
    ]
    
    new_incs = list_incidents(mock_redis, state_filter="NEW")
    assert len(new_incs) == 1
    assert new_incs[0]["incident_id"] == "inc-1"


def test_idle_incident_dump_and_ttl_lifecycle(tmp_path):
    import redis
    from shared.config.schemas import LedgerConfig
    from shared.models.ledger import LedgerEntry
    from services.api_server.incident_store import sweep_idle_incidents_once

    class InMemoryRedis(redis.Redis):
        def __init__(self):
            self.kv = {}
            self.lists = {}
            self.ttls = {}

        def get(self, name):
            return self.kv.get(name)

        def set(self, name, value, *args, **kwargs):
            self.kv[name] = value if isinstance(value, (bytes, bytearray)) else str(value).encode("utf-8")
            self.ttls.setdefault(name, -1)
            return True

        def lrange(self, name, start, end):
            items = self.lists.get(name, [])
            return items if end == -1 else items[start : end + 1]

        def scan_iter(self, match=None):
            import fnmatch
            pat = match or "*"
            return [k for k in list(self.kv.keys()) if fnmatch.fnmatch(k, pat)]

        def expire(self, name, time):
            if name in self.kv or name in self.lists:
                self.ttls[name] = time
                return True
            return False

        def ttl(self, name):
            if name not in self.kv and name not in self.lists:
                return -2
            return self.ttls.get(name, -1)

    r = InMemoryRedis()
    inc_id = "inc-20261117-0001"
    old_ts = "2026-11-17T10:00:00+00:00"
    now_ts = datetime(2026, 11, 17, 11, 0, 0, tzinfo=timezone.utc).timestamp()  # 3600s idle

    r.set(
        f"incident:{inc_id}:state",
        json.dumps({"incident_id": inc_id, "state": "ESCALATED", "updated_at": old_ts}),
    )
    r.set(
        f"incident:{inc_id}:context",
        json.dumps({"primary_hypothesis": "High CPU on checkout"}),
    )
    r.set(
        f"incident:{inc_id}:action:act-1",
        json.dumps({"action_id": "act-1", "status": "denied"}),
    )
    entry = LedgerEntry(
        entry_id="evt-1",
        incident_id=inc_id,
        state=IncidentState.NEW,
        event_type="alert_received",
        source="alertmanager",
    )
    r.lists[f"incident:{inc_id}:ledger"] = [entry.model_dump_json().encode("utf-8")]

    ledger_cfg = LedgerConfig(
        dump_on_close=True,
        dump_idle_seconds=1800,
        redis_ttl_seconds=43200,
        storage_path=str(tmp_path),
    )

    archived = sweep_idle_incidents_once(r, ledger_config=ledger_cfg, now_ts=now_ts)
    assert archived == [inc_id]

    # Verify files flushed to disk
    inc_dir = tmp_path / inc_id
    assert (inc_dir / "ledger_full.json").is_file()
    snapshot_file = inc_dir / "incident_snapshot.json"
    assert snapshot_file.is_file()
    snap = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert snap["context"]["primary_hypothesis"] == "High CPU on checkout"
    assert snap["state"]["state"] == "ESCALATED"

    # Verify redis_ttl_seconds applied across all keys
    assert r.ttls[f"incident:{inc_id}:state"] == 43200
    assert r.ttls[f"incident:{inc_id}:ledger"] == 43200
    assert r.ttls[f"incident:{inc_id}:context"] == 43200
    assert r.ttls[f"incident:{inc_id}:action:act-1"] == 43200
    assert r.ttls[f"incident:{inc_id}:dumped_at"] == 43200

    # Second sweep at same timestamp must be idempotent (no duplicate dump)
    archived_again = sweep_idle_incidents_once(r, ledger_config=ledger_cfg, now_ts=now_ts)
    assert archived_again == []
