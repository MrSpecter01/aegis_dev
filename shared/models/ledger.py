from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from shared.models.enums import IncidentState


class LedgerEntry(BaseModel):
    entry_id: str
    incident_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state: IncidentState
    event_type: str
    source: str
    details: Dict[str, Any] = Field(default_factory=dict)


class IncidentReport(BaseModel):
    incident_id: str
    title: str
    severity: str
    affected_service: str
    affected_namespace: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    root_cause_summary: str
    remediation_summary: str
    timeline: List[LedgerEntry] = Field(default_factory=list)
    action_log: List[Dict[str, Any]] = Field(default_factory=list)
    preventative_recommendations: List[str] = Field(default_factory=list)
    markdown_content: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

