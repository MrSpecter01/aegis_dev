import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import redis
from transitions import Machine

from shared.models.enums import IncidentState
from shared.models.ledger import LedgerEntry
from shared.ledger.writer import append_ledger_entry
from services.api_server.incident_store import get_incident, update_incident_state

logger = logging.getLogger(__name__)

STATES = [state.value for state in IncidentState]

TRANSITIONS = [
    {'trigger': 'alert_received',        'source': 'NEW',                'dest': 'GROUPING'},
    {'trigger': 'grouping_complete',     'source': 'GROUPING',           'dest': 'TRIAGE'},
    {'trigger': 'triage_complete',       'source': 'TRIAGE',             'dest': 'MERGING'},
    {'trigger': 'merge_complete',        'source': 'MERGING',            'dest': 'REMEDIATION'},
    {'trigger': 'action_proposed',       'source': 'REMEDIATION',        'dest': 'AWAITING_APPROVAL'},
    {'trigger': 'approved',              'source': ['AWAITING_APPROVAL', 'ESCALATED'], 'dest': 'EXECUTING'},
    {'trigger': 'denied',                'source': ['AWAITING_APPROVAL', 'EXECUTING'], 'dest': 'ESCALATED'},
    {'trigger': 'execution_success',     'source': 'EXECUTING',          'dest': 'MONITORING'},
    {'trigger': 'execution_failed',      'source': 'EXECUTING',          'dest': 'ESCALATED'},
    {'trigger': 'pre_exec_replan',       'source': 'EXECUTING',          'dest': 'REMEDIATION'},
    {'trigger': 'signals_converged',     'source': ['MONITORING', 'EXECUTING'], 'dest': 'REPORTING'},
    {'trigger': 'signals_not_converged', 'source': ['MONITORING', 'EXECUTING'], 'dest': 'REMEDIATION'},
    {'trigger': 'retry_limit_reached',   'source': 'MONITORING',         'dest': 'ESCALATED'},
    {'trigger': 'report_delivered',      'source': 'REPORTING',          'dest': 'CLOSED'},
    {'trigger': 'generate_report',       'source': ['ESCALATED', 'AWAITING_APPROVAL', 'EXECUTING', 'MONITORING', 'REMEDIATION', 'STANDBY', 'TRIAGE', 'MERGING'], 'dest': 'REPORTING'},
    {'trigger': 'escalate',              'source': '*',                  'dest': 'ESCALATED'},
    {'trigger': 'force_close',           'source': '*',                  'dest': 'CLOSED'},
    {'trigger': 'drop',                  'source': '*',                  'dest': 'CLOSED'},
    {'trigger': 'steer',                 'source': ['AWAITING_APPROVAL', 'ESCALATED', 'REMEDIATION', 'STANDBY'], 'dest': 'REMEDIATION'},
    {'trigger': 'pause',                 'source': ['NEW', 'GROUPING', 'TRIAGE', 'MERGING', 'REMEDIATION', 'AWAITING_APPROVAL', 'EXECUTING', 'MONITORING'], 'dest': 'STANDBY'},
    {'trigger': 'resume',                'source': 'STANDBY',            'dest': 'REMEDIATION'},
    {'trigger': 'resume_executing',     'source': 'STANDBY',            'dest': 'EXECUTING'},
    {'trigger': 'resume_monitoring',    'source': 'STANDBY',            'dest': 'MONITORING'},
    {'trigger': 'resume_approval',      'source': 'STANDBY',            'dest': 'AWAITING_APPROVAL'},
    {'trigger': 'resume_triage',        'source': 'STANDBY',            'dest': 'TRIAGE'},
]

class IncidentMachine:
    def __init__(self, incident_id: str, redis_client: Optional[redis.Redis], state: str = "NEW"):
        self.incident_id = incident_id
        self.redis_client = redis_client
        self.machine = Machine(
            model=self,
            states=STATES,
            transitions=TRANSITIONS,
            initial=state,
            auto_transitions=False,
            after_state_change='_on_enter_state'
        )

    async def trigger(self, event: str, actor: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if self.state == "CLOSED":
            raise ValueError(f"Cannot transition from terminal state {self.state}")
        if self.state == "ESCALATED" and event not in ("steer", "force_close", "approved", "drop", "generate_report"):
            raise ValueError(f"Cannot transition from terminal state {self.state}")
        valid_standby_events = (
            "resume", "resume_executing", "resume_monitoring", "resume_approval",
            "resume_triage", "steer", "escalate", "force_close", "drop", "generate_report"
        )
        if self.state == "STANDBY" and event not in valid_standby_events:
            raise ValueError(f"Cannot transition from STANDBY state with event {event}")
            
        if metadata is None:
            metadata = {}

        self._current_actor = actor
        self._current_event = event
        self._current_metadata = metadata
        self._from_state = self.state

        # Use the underlying transitions trigger mechanism
        try:
            from transitions.core import MachineError
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            self._loop = loop
            # Run the synchronous trigger (and its synchronous I/O callbacks) in a thread executor
            try:
                return await loop.run_in_executor(None, getattr(self, event))
            except MachineError as e:
                raise ValueError(f"Invalid event {event} for state {self.state}") from e
        except AttributeError:
            raise ValueError(f"Invalid event {event} for state {self.state}")

    def _on_enter_state(self):
        to_state = IncidentState(self.state)
        from_state = IncidentState(self._from_state)
        
        from services.api_server.routers.system import aegis_incident_state_transitions_total
        aegis_incident_state_transitions_total.labels(from_state=from_state.value, to_state=to_state.value).inc()
        
        entry = LedgerEntry(
            entry_id=f"evt-{datetime.now(timezone.utc).timestamp()}-{self._current_event}",
            incident_id=self.incident_id,
            state=to_state,
            event_type=self._current_event,
            source=self._current_actor,
            details=self._current_metadata
        )
        
        append_ledger_entry(self.redis_client, self.incident_id, entry)
        
        if self.redis_client:
            if isinstance(self.redis_client, redis.Redis):
                update_incident_state(self.redis_client, self.incident_id, to_state.value, from_state=from_state.value)
            
            # Dispatch to streams
            if to_state == IncidentState.TRIAGE:
                if isinstance(self.redis_client, redis.Redis):
                    job_payload = {"incident_id": self.incident_id}
                    try:
                        raw_grp = self.redis_client.get(f"incident:{self.incident_id}:alert_group")
                        if raw_grp and isinstance(raw_grp, (str, bytes, bytearray)):
                            job_payload["alert_metadata"] = raw_grp.decode("utf-8") if isinstance(raw_grp, bytes) else str(raw_grp)
                        inc_data = get_incident(self.redis_client, self.incident_id) or {}
                        svc = inc_data.get("affected_service")
                        if svc:
                            from services.api_server.topology.store import TopologyStore
                            from services.api_server.topology.query import get_subgraph
                            store = TopologyStore(self.redis_client)
                            subgraph = get_subgraph(store, svc)
                            job_payload["topology_subgraph"] = subgraph.model_dump_json()
                    except Exception as exc:
                        logger.warning("Failed to enrich Phase 1 job payload: %s", exc)
                    self.redis_client.xadd("stream:phase1:jobs", job_payload)
            elif to_state in (IncidentState.REMEDIATION, IncidentState.EXECUTING):
                # When transitioning to REMEDIATION via pre_exec_replan, the Phase 2 worker
                # is already actively executing the replan in-memory. Suppress duplicate stream dispatch.
                if to_state == IncidentState.REMEDIATION and self._current_event == "pre_exec_replan":
                    logger.info("Incident %s transitioned to REMEDIATION via pre_exec_replan; suppressing duplicate stream job", self.incident_id)
                elif isinstance(self.redis_client, redis.Redis):
                    job_payload = {"incident_id": self.incident_id}
                    if to_state == IncidentState.EXECUTING:
                        job_payload["approval_status"] = "approved"
                        if self._current_metadata and self._current_metadata.get("action_id"):
                            job_payload["action_id"] = str(self._current_metadata["action_id"])
                    self.redis_client.xadd("stream:phase2:jobs", job_payload)
            elif to_state == IncidentState.REPORTING:
                if isinstance(self.redis_client, redis.Redis):
                    self.redis_client.xadd("stream:phase3:jobs", {"incident_id": self.incident_id})
            elif to_state == IncidentState.STANDBY:
                if isinstance(self.redis_client, redis.Redis):
                    self.redis_client.set(f"incident:{self.incident_id}:pre_pause_state", from_state.value)
            if self._current_event == "drop" and isinstance(self.redis_client, redis.Redis):
                self.redis_client.set(f"incident:{self.incident_id}:dropped", "1")

        # Notification dispatch (fire-and-forget)
        try:
            from services.api_server.notifications import dispatch_notification_background

            incident_data = get_incident(self.redis_client, self.incident_id) or {}
            event_loop = getattr(self, "_loop", None)

            if to_state == IncidentState.AWAITING_APPROVAL:
                action_data = {}
                if self.redis_client:
                    raw_action = self.redis_client.get(f"incident:{self.incident_id}:action")
                    if raw_action:
                        if isinstance(raw_action, (bytes, bytearray)):
                            raw_action = raw_action.decode("utf-8")
                        try:
                            action_data = json.loads(raw_action)
                        except Exception as exc:
                            logger.warning("Failed to decode raw action for incident %s: %s", self.incident_id, exc, exc_info=True)
                dispatch_notification_background(
                    event="approval_request",
                    incident_id=self.incident_id,
                    data={"incident": incident_data, "action": action_data},
                    loop=event_loop,
                )
            elif to_state == IncidentState.REMEDIATION and from_state == IncidentState.MERGING:
                findings_data = {}
                if self.redis_client:
                    raw_ctx = self.redis_client.get(f"incident:{self.incident_id}:context")
                    if raw_ctx:
                        if isinstance(raw_ctx, (bytes, bytearray)):
                            raw_ctx = raw_ctx.decode("utf-8")
                        try:
                            ctx_dict = json.loads(raw_ctx)
                            findings_data = ctx_dict.get("merged_findings") or ctx_dict
                        except Exception as exc:
                            logger.warning("Failed to decode raw context for incident %s: %s", self.incident_id, exc, exc_info=True)
                dispatch_notification_background(
                    event="triage_summary",
                    incident_id=self.incident_id,
                    data={"incident": incident_data, "merged_findings": findings_data},
                    loop=event_loop,
                )
            elif to_state == IncidentState.ESCALATED:
                reason = self._current_metadata.get("reason", self._current_event)
                dispatch_notification_background(
                    event="escalation",
                    incident_id=self.incident_id,
                    data={"incident": incident_data, "reason": reason},
                    loop=event_loop,
                )
            elif to_state == IncidentState.CLOSED and from_state == IncidentState.REPORTING:
                dispatch_notification_background(
                    event="report_delivered",
                    incident_id=self.incident_id,
                    data={
                        "incident": incident_data,
                        "report_path": self._current_metadata.get("report_path", f"/data/incidents/{self.incident_id}/report.md"),
                    },
                    loop=event_loop,
                )
        except Exception as exc:
            logger.warning("notification dispatch failed for %s: %s", self.incident_id, exc, exc_info=True)

def load_machine(incident_id: str, redis_client: Optional[redis.Redis]) -> IncidentMachine:
    from services.api_server.incident_store import get_incident
    data = get_incident(redis_client, incident_id)
    state = data.get("state", "NEW") if data else "NEW"
    return IncidentMachine(incident_id, redis_client, state)

def save_machine(incident_id: str, state: str, redis_client: Optional[redis.Redis]):
    from services.api_server.incident_store import update_incident_state
    if redis_client and isinstance(redis_client, redis.Redis):
        update_incident_state(redis_client, incident_id, state)
