from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Dict, List, Optional, Any, Set, Tuple

from shared.config.schemas import IngestionConfig
from shared.models.alerts import Alert, AlertGroup
from shared.models.enums import Severity
from services.api_server.incident_store import (
    generate_incident_id,
    create_incident,
    get_incident,
)
from services.api_server.state_machine import load_machine
from services.api_server.topology.store import TopologyStore
from services.api_server.topology.query import get_adjacent_services, get_node

logger = logging.getLogger("aegis.ingestion.correlator")


class AlertCorrelator:
    """
    Ingestion & Correlation Engine.
    Deduplicates, flap-detects, groups by topology adjacency within time windows,
    assigns severities, and drives state machine transitions.
    """

    def __init__(
        self,
        redis_client: Any,
        config: IngestionConfig,
        topology_store: Optional[TopologyStore] = None,
        allowed_namespaces: Optional[List[str]] = None,
    ) -> None:
        self.redis = redis_client
        self.config = config
        self.topology_store = topology_store
        self.allowed_namespaces: Optional[List[str]] = (
            [str(ns).strip().lower() for ns in allowed_namespaces if str(ns).strip()]
            if isinstance(allowed_namespaces, list) and allowed_namespaces
            else None
        )

    def _map_severity(self, alert: Alert) -> Severity:
        """Map raw Alertmanager label severity to internal Severity enum."""
        raw_sev = str(alert.labels.get("severity", "")).lower().strip()
        if raw_sev in self.config.severity_map:
            sev_str = self.config.severity_map[raw_sev]
            try:
                return Severity(sev_str)
            except ValueError as exc:
                logger.warning("Invalid mapped severity '%s' for raw severity '%s': %s", sev_str, raw_sev, exc)
        if raw_sev.upper() in Severity.__members__:
            return Severity(raw_sev.upper())
        return Severity.P3

    def _compute_fingerprint(self, alert: Alert) -> str:
        """Get or compute a stable fingerprint string for an alert."""
        if alert.fingerprint:
            return alert.fingerprint
        key_str = f"{alert.alertname}:{alert.service}:{alert.namespace}"
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]

    def _check_flapping(self, fingerprint: str) -> bool:
        """
        Increment flap counter for fingerprint.
        Returns True if alert is flapping (> flap_threshold within flap_window_seconds).
        """
        key = f"flap:count:{fingerprint}"
        count = self.redis.incr(key)
        if count == 1:
            self.redis.expire(key, self.config.flap_window_seconds)
        return count > self.config.flap_threshold

    def get_alert_group(self, incident_id: str) -> Optional[AlertGroup]:
        """Fetch AlertGroup for an incident from Redis."""
        raw = self.redis.get(f"incident:{incident_id}:alert_group")
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return AlertGroup.model_validate_json(raw)
        except Exception as e:
            logger.error(f"Failed to deserialise AlertGroup for {incident_id}: {e}")
            return None

    def save_alert_group(self, incident_id: str, group: AlertGroup) -> None:
        """Persist AlertGroup for an incident to Redis."""
        self.redis.set(f"incident:{incident_id}:alert_group", group.model_dump_json())

    def _collect_open_namespace_incidents(self, namespace: str, ns_key: str) -> Set[str]:
        """Load active candidate IDs and recover any open incidents in Redis missing from active:namespace."""
        active_candidates: Set[str] = set()

        raw_candidates = self.redis.smembers(ns_key)
        if raw_candidates:
            for item in raw_candidates:
                if isinstance(item, bytes):
                    item = item.decode("utf-8")
                active_candidates.add(item)

        try:
            for raw_key in self.redis.scan_iter("incident:*"):
                key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
                if not key.endswith(":state"):
                    continue
                cand_id = key[len("incident:"):-len(":state")]
                if not cand_id or cand_id in active_candidates:
                    continue
                cand_inc = get_incident(self.redis, cand_id)
                if not cand_inc or cand_inc.get("state") == "CLOSED":
                    continue
                cand_ns = cand_inc.get("affected_namespace")
                if not cand_ns:
                    cand_group = self.get_alert_group(cand_id)
                    cand_ns = cand_group.affected_namespace if cand_group else None
                if cand_ns == namespace:
                    active_candidates.add(cand_id)
                    self.redis.sadd(ns_key, cand_id)
        except Exception as exc:
            logger.debug("Open incident scan failed for namespace %s: %s", namespace, exc)

        return active_candidates

    def _record_and_check_recurrence(
        self,
        alert: Alert,
        incident_id: str,
        now: datetime,
    ) -> Tuple[bool, int, List[Dict[str, Any]]]:
        """Track recurring incidents within recurrence_window_seconds, pruning stale entries."""
        window_seconds = max(1, int(getattr(self.config, "recurrence_window_seconds", 900)))
        threshold = max(2, int(getattr(self.config, "recurrence_threshold", 3)))
        rec_key = f"recurrence:history:{alert.namespace}:{alert.service}:{alert.alertname}"
        now_ts = now.timestamp()

        history: List[Dict[str, Any]] = []
        raw_hist = self.redis.get(rec_key)
        if raw_hist:
            if isinstance(raw_hist, bytes):
                raw_hist = raw_hist.decode("utf-8")
            try:
                parsed = json.loads(raw_hist)
                if isinstance(parsed, list):
                    history = parsed
            except Exception:
                history = []

        # Prune stale history outside recurrence_window_seconds & store bounded causal metadata
        allowed_fields = (
            "incident_id", "timestamp", "alertname", "service",
            "state", "outcome", "hypothesis", "last_action_tool", "action_rationale",
        )
        fresh_history: List[Dict[str, Any]] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            try:
                ts = float(entry.get("timestamp", 0.0))
            except (TypeError, ValueError):
                continue
            if (now_ts - ts) <= window_seconds:
                sanitized = {k: entry[k] for k in allowed_fields if k in entry}
                prev_id = sanitized.get("incident_id")
                if prev_id and prev_id != incident_id:
                    prev_inc = get_incident(self.redis, str(prev_id))
                    if prev_inc and prev_inc.get("state"):
                        sanitized["state"] = prev_inc["state"]
                    if "hypothesis" not in sanitized:
                        try:
                            raw_ctx = self.redis.get(f"incident:{prev_id}:context")
                            if raw_ctx:
                                if isinstance(raw_ctx, bytes):
                                    raw_ctx = raw_ctx.decode("utf-8")
                                ctx_obj = json.loads(raw_ctx)
                                hyp_val = str(ctx_obj.get("primary_hypothesis") or "").strip()
                                if hyp_val:
                                    sanitized["hypothesis"] = hyp_val
                        except Exception:
                            pass
                    if "last_action_tool" not in sanitized or "action_rationale" not in sanitized:
                        try:
                            raw_prop = self.redis.get(f"incident:{prev_id}:proposal")
                            if raw_prop:
                                if isinstance(raw_prop, bytes):
                                    raw_prop = raw_prop.decode("utf-8")
                                prop_obj = json.loads(raw_prop)
                                acts = prop_obj.get("actions") or []
                                if acts and isinstance(acts[0], dict):
                                    if "last_action_tool" not in sanitized and acts[0].get("tool_name"):
                                        sanitized["last_action_tool"] = str(acts[0]["tool_name"])
                                    if "action_rationale" not in sanitized and acts[0].get("rationale"):
                                        sanitized["action_rationale"] = str(acts[0]["rationale"])
                        except Exception:
                            pass
                if "hypothesis" in sanitized:
                    sanitized["hypothesis"] = str(sanitized["hypothesis"])
                if "action_rationale" in sanitized:
                    sanitized["action_rationale"] = str(sanitized["action_rationale"])
                fresh_history.append(sanitized)

        prior_within_window = [dict(e) for e in fresh_history if e.get("incident_id") != incident_id]

        fresh_history.append({
            "incident_id": incident_id,
            "timestamp": now_ts,
            "alertname": alert.alertname,
            "service": alert.service,
            "state": "NEW",
        })
        self.redis.set(rec_key, json.dumps(fresh_history))
        self.redis.expire(rec_key, window_seconds)

        recurrence_count = len(fresh_history)
        is_chronic = recurrence_count >= threshold
        return is_chronic, recurrence_count, prior_within_window[-3:]

    async def process(
        self,
        raw_payload: Dict[str, Any],
        ingest_meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Process a raw Alertmanager webhook payload.
        Returns incident_id if processed/merged, or None if suppressed due to flapping.
        """
        alerts_data = raw_payload.get("alerts", [])
        if not alerts_data:
            logger.warning("Received empty alerts payload")
            return None

        parsed_alerts = [Alert.model_validate(a) for a in alerts_data]
        firing_alerts = [a for a in parsed_alerts if str(a.status).lower() != "resolved"]
        if not firing_alerts:
            # Never create a new incident from resolved-only Alertmanager webhooks.
            for resolved_alert in parsed_alerts:
                fp = self._compute_fingerprint(resolved_alert)
                existing_inc_id = self.redis.get(f"incident:fingerprints:{fp}")
                if existing_inc_id:
                    if isinstance(existing_inc_id, bytes):
                        existing_inc_id = existing_inc_id.decode("utf-8")
                    inc = get_incident(self.redis, existing_inc_id)
                    if inc and inc.get("state") != "CLOSED":
                        logger.info(
                            "Received resolved alert %s for open incident %s",
                            fp,
                            existing_inc_id,
                        )
                        return existing_inc_id
            logger.info("Ignoring resolved-only alert payload with no open incident")
            return None

        if self.allowed_namespaces:
            from shared.audit.tracker import record_dropped_alert_async
            allowed_set = set(self.allowed_namespaces)
            in_scope: List[Alert] = []
            for alert_item in firing_alerts:
                if alert_item.namespace.strip().lower() in allowed_set:
                    in_scope.append(alert_item)
                else:
                    await record_dropped_alert_async(
                        self.redis,
                        alertname=alert_item.alertname,
                        service=alert_item.service,
                        namespace=alert_item.namespace,
                        allowed_namespaces=self.allowed_namespaces,
                        fingerprint=alert_item.fingerprint or "",
                        ingested_by=(ingest_meta or {}).get("ingested_by", ""),
                        key_id=(ingest_meta or {}).get("ingested_key_id", ""),
                        reason="namespace_not_monitored",
                    )
            firing_alerts = in_scope
            if not firing_alerts:
                return None

        first_alert = firing_alerts[0]
        additional_alerts = firing_alerts[1:]
        fingerprint = self._compute_fingerprint(first_alert)
        severity = self._map_severity(first_alert)

        # 1. Flap detection
        if self._check_flapping(fingerprint):
            logger.warning(
                f"Alert {fingerprint} for {first_alert.service} is flapping. Suppressing incident cycle."
            )
            existing_inc_id = self.redis.get(f"incident:fingerprints:{fingerprint}")
            if existing_inc_id:
                if isinstance(existing_inc_id, bytes):
                    existing_inc_id = existing_inc_id.decode("utf-8")
                grp = self.get_alert_group(existing_inc_id)
                if grp:
                    grp.flapping = True
                    self.save_alert_group(existing_inc_id, grp)
            return None

        # 2. Deduplication check (ignore closed/escalated/reporting incidents so re-alerts open fresh incidents)
        existing_inc_id = self.redis.get(f"incident:fingerprints:{fingerprint}")
        if existing_inc_id:
            if isinstance(existing_inc_id, bytes):
                existing_inc_id = existing_inc_id.decode("utf-8")
            grp = self.get_alert_group(existing_inc_id)
            inc = get_incident(self.redis, existing_inc_id)
            inc_state = str(inc.get("state") or "") if inc else ""
            if grp and inc and inc_state not in ("CLOSED", "ESCALATED", "REPORTING"):
                known = {a.fingerprint or self._compute_fingerprint(a) for a in [grp.primary_alert, *grp.related_alerts]}
                for a in [first_alert, *additional_alerts]:
                    if (a.fingerprint or self._compute_fingerprint(a)) not in known:
                        grp.related_alerts.append(a)
                if fingerprint not in grp.fingerprints:
                    grp.fingerprints.append(fingerprint)
                self.save_alert_group(existing_inc_id, grp)
                self.redis.expire(f"incident:fingerprints:{fingerprint}", self.config.fingerprint_ttl_seconds)
                logger.info(f"Deduplicated alert {fingerprint} into existing incident {existing_inc_id}")
                return existing_inc_id

        # 3. Origin-anchored correlation within fixed window + awaiting/mutation handling
        namespace = first_alert.namespace
        ns_key = f"active:namespace:{namespace}"
        active_candidates = self._collect_open_namespace_incidents(namespace, ns_key)

        now = datetime.now(timezone.utc)
        matched_incident_id: Optional[str] = None
        mutation_delta_msg: Optional[str] = None
        matched_state: str = ""
        max_hops = max(1, int(getattr(self.config, "topology_max_hops", 2)))
        curr_service = first_alert.service
        curr_adj = (
            get_adjacent_services(self.topology_store, curr_service, max_hops=max_hops)
            if self.topology_store
            else frozenset()
        )
        cascade_states = ("", "NEW", "GROUPING", "TRIAGE", "MERGING", "REMEDIATION")
        hold_states = ("AWAITING_APPROVAL", "REMEDIATION", "EXECUTING", "MONITORING", "STANDBY")

        for cand_id in sorted(active_candidates):
            cand_inc = get_incident(self.redis, cand_id)
            cand_state = str(cand_inc.get("state") or "") if cand_inc else ""
            if not cand_inc or cand_state in ("CLOSED", "ESCALATED", "REPORTING"):
                self.redis.srem(ns_key, cand_id)
                continue

            cand_group = self.get_alert_group(cand_id)
            if not cand_group:
                self.redis.srem(ns_key, cand_id)
                continue

            # Fixed window measured from incident creation time (never reset on merge)
            age = (now - cand_group.created_at).total_seconds()
            origin_service = cand_group.affected_service or cand_group.primary_alert.service
            cand_known_services = {origin_service} | {
                a.service
                for a in [cand_group.primary_alert, *cand_group.related_alerts]
                if a.service
            }
            cand_alertnames = {
                a.alertname
                for a in [cand_group.primary_alert, *cand_group.related_alerts]
                if a.alertname
            }

            if age <= self.config.correlation_window_seconds and cand_state in cascade_states:
                # 2-hop topology matching anchored strictly on origin_service to prevent transitive creep
                if curr_service == origin_service:
                    matched_incident_id = cand_id
                    matched_state = cand_state
                    break

                if self.topology_store:
                    origin_adj = get_adjacent_services(self.topology_store, origin_service, max_hops=max_hops)
                    if (
                        origin_service in curr_adj
                        or f"{origin_service}.{namespace}" in curr_adj
                        or curr_service in origin_adj
                        or f"{curr_service}.{namespace}" in origin_adj
                    ):
                        matched_incident_id = cand_id
                        matched_state = cand_state
                        break

            # Past fixed 180s window OR waiting on approval/execution/monitoring
            if cand_state in hold_states:
                if curr_service in cand_known_services and first_alert.alertname in cand_alertnames:
                    matched_incident_id = cand_id
                    matched_state = cand_state
                    break

                if (
                    curr_service == origin_service
                    and first_alert.alertname not in cand_alertnames
                    and cand_state in ("AWAITING_APPROVAL", "REMEDIATION")
                ):
                    matched_incident_id = cand_id
                    matched_state = cand_state
                    mutation_delta_msg = (
                        f"Alert signature on origin service '{origin_service}' mutated from "
                        f"{sorted(cand_alertnames)} to '{first_alert.alertname}' while in state {cand_state}."
                    )
                    break

            if age > self.config.correlation_window_seconds * 2 and cand_state not in ("AWAITING_APPROVAL", "EXECUTING", "MONITORING", "STANDBY"):
                self.redis.srem(ns_key, cand_id)

        if matched_incident_id:
            # Group into matched active incident without resetting created_at
            matched_group = self.get_alert_group(matched_incident_id)
            if matched_group:
                matched_group.related_alerts.append(first_alert)
                matched_group.related_alerts.extend(additional_alerts)
                if fingerprint not in matched_group.fingerprints:
                    matched_group.fingerprints.append(fingerprint)

                # Escalate severity if incoming is higher priority (P1 > P2 > P3)
                sev_order = {"P1": 1, "P2": 2, "P3": 3}
                if sev_order.get(severity.value, 3) < sev_order.get(matched_group.severity.value, 3):
                    matched_group.severity = severity

                self.save_alert_group(matched_incident_id, matched_group)
                self.redis.set(f"incident:fingerprints:{fingerprint}", matched_incident_id)
                self.redis.expire(f"incident:fingerprints:{fingerprint}", self.config.fingerprint_ttl_seconds)
                self.redis.expire(ns_key, self.config.correlation_window_seconds * 2)

                if mutation_delta_msg:
                    self.redis.set(f"incident:{matched_incident_id}:mutation_delta", mutation_delta_msg)
                    logger.warning("Incident %s mutation detected by correlator: %s", matched_incident_id, mutation_delta_msg)
                    if matched_state == "AWAITING_APPROVAL":
                        try:
                            m_inc = load_machine(matched_incident_id, self.redis)
                            if m_inc.state == "AWAITING_APPROVAL":
                                await m_inc.trigger(
                                    "steer",
                                    "correlator",
                                    {"mutation_delta": mutation_delta_msg, "new_alertname": first_alert.alertname},
                                )
                        except Exception as steer_exc:
                            logger.warning(
                                "Failed to trigger mutation re-plan on %s: %s",
                                matched_incident_id,
                                steer_exc,
                            )

                logger.info(
                    f"Correlated alert {fingerprint} ({first_alert.service}) into incident {matched_incident_id}"
                )
                return matched_incident_id

        # 4. Correlation Miss -> Create new incident
        # Check if service is known in topology
        correlation_warning = False
        if self.topology_store:
            node = get_node(self.topology_store, first_alert.service)
            if node is None:
                correlation_warning = True

        all_fps = [fingerprint]
        for a in additional_alerts:
            fp = self._compute_fingerprint(a)
            if fp not in all_fps:
                all_fps.append(fp)

        incident_id = generate_incident_id(self.redis)
        is_chronic, rec_count, recent_recs = self._record_and_check_recurrence(
            first_alert,
            incident_id,
            now,
        )

        alert_group = AlertGroup(
            group_id=raw_payload.get("groupKey", f"group-{fingerprint}"),
            primary_alert=first_alert,
            related_alerts=additional_alerts,
            severity=severity,
            affected_service=first_alert.service,
            affected_namespace=first_alert.namespace,
            flapping=False,
            correlation_warning=correlation_warning,
            chronic_recurrence=is_chronic,
            recurrence_count=rec_count,
            recent_recurrences=recent_recs,
            fingerprints=all_fps,
            ingested_by=(ingest_meta or {}).get("ingested_by"),
            ingested_key_id=(ingest_meta or {}).get("ingested_key_id"),
            ingested_entity_type=(ingest_meta or {}).get("ingested_entity_type"),
        )

        create_incident(self.redis, incident_id, alert_group)
        self.save_alert_group(incident_id, alert_group)

        # Index fingerprint and active namespace
        self.redis.set(f"incident:fingerprints:{fingerprint}", incident_id)
        self.redis.expire(f"incident:fingerprints:{fingerprint}", self.config.fingerprint_ttl_seconds)
        self.redis.sadd(ns_key, incident_id)
        self.redis.expire(ns_key, self.config.correlation_window_seconds * 2)

        # Drive state machine transitions: NEW -> GROUPING -> TRIAGE
        machine = load_machine(incident_id, self.redis)
        alert_received_meta: Dict[str, Any] = {"alert_group": alert_group.model_dump(mode="json")}
        if ingest_meta:
            alert_received_meta["ingest_meta"] = ingest_meta
        if machine.state == "NEW":
            await machine.trigger(
                "alert_received",
                (ingest_meta or {}).get("ingested_by") or "correlator",
                alert_received_meta,
            )

        if machine.state == "GROUPING":
            await machine.trigger(
                "grouping_complete",
                "correlator",
                {"incident_id": incident_id},
            )

        if ingest_meta:
            from shared.audit.tracker import record_interaction_async
            await record_interaction_async(
                self.redis,
                direction="inbound",
                category="alert_incident_created",
                actor=ingest_meta.get("ingested_by") or "webhook",
                entity_type=ingest_meta.get("ingested_entity_type") or "webhook",
                key_id=ingest_meta.get("ingested_key_id"),
                target=f"incident:{incident_id}",
                action="create_incident",
                external=True,
                incident_id=incident_id,
                details={"service": first_alert.service, "severity": severity.value},
            )

        inc_record = get_incident(self.redis, incident_id)
        from services.api_server.notifications import dispatch_notification_background
        dispatch_notification_background(
            event="incident_created",
            incident_id=incident_id,
            data={"incident": inc_record or {}},
        )

        logger.info(
            f"Created incident {incident_id} for alert {fingerprint} ({first_alert.service}), state: {machine.state}"
        )
        return incident_id
