from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from shared.models.enums import Severity


class Alert(BaseModel):
    fingerprint: Optional[str] = None
    status: str = "firing"
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[datetime] = None
    endsAt: Optional[datetime] = None
    generatorURL: Optional[str] = None

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "UnknownAlert")

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace", "default")

    @property
    def pod_name(self) -> Optional[str]:
        return self.labels.get("pod") or self.labels.get("pod_name")

    @property
    def service(self) -> str:
        svc = (
            self.labels.get("service")
            or self.labels.get("service_name")
            or self.labels.get("app")
            or self.labels.get("deployment")
        )
        if svc:
            return svc
        pod = self.pod_name
        if pod:
            import re
            # Strip Deployment ReplicaSet+Pod suffix, DaemonSet/Job hash with digit, or StatefulSet ordinal
            normalized = re.sub(
                r"(?:-[a-z0-9]{6,10}-[a-z0-9]{5}|-(?=[a-z0-9]*\d)[a-z0-9]{5}|-\d+)$",
                "",
                pod,
            )
            return normalized or pod
        return "unknown"

    @property
    def severity(self) -> Severity:
        raw_sev = self.labels.get("severity", "P3").upper()
        if raw_sev in Severity.__members__:
            return Severity(raw_sev)
        return Severity.P3


class AlertGroup(BaseModel):
    group_id: str
    primary_alert: Alert
    related_alerts: List[Alert] = Field(default_factory=list)
    severity: Severity = Severity.P3
    affected_service: str
    affected_namespace: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    flapping: bool = False
    correlation_warning: bool = False
    chronic_recurrence: bool = False
    recurrence_count: int = 0
    recent_recurrences: List[Dict[str, Any]] = Field(default_factory=list)
    fingerprints: List[str] = Field(default_factory=list)
    ingested_by: Optional[str] = None
    ingested_key_id: Optional[str] = None
    ingested_entity_type: Optional[str] = None

    @property
    def affected_pods(self) -> List[str]:
        pods: List[str] = []
        for a in [self.primary_alert, *self.related_alerts]:
            p = a.pod_name
            if p and p not in pods:
                pods.append(p)
        return pods

