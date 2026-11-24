"""Notification models for inter-service event dispatch to notification channels (Telegram, Dashboard)."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict
from pydantic import BaseModel, Field


class NotificationEvent(str, Enum):
    INCIDENT_CREATED = "incident_created"
    TRIAGE_SUMMARY = "triage_summary"
    APPROVAL_REQUEST = "approval_request"
    DENIAL_NOTIFICATION = "denial_notification"
    ESCALATION = "escalation"
    REPORT_DELIVERED = "report_delivered"


class NotificationPayload(BaseModel):
    event: str
    incident_id: str
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
