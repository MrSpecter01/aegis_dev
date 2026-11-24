from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator

from shared.models.enums import AgentRole


class EvidenceItem(BaseModel):
    source_tool: str = "unknown"
    query_or_cmd: str = ""
    raw_output: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def normalize_evidence_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("query_or_cmd"):
                data["query_or_cmd"] = data.get("query") or data.get("cmd") or data.get("command") or ""
            if not data.get("raw_output"):
                data["raw_output"] = data.get("output") or data.get("result") or ""
        return data


class AgentFinding(BaseModel):
    agent_role: AgentRole
    hypothesis: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    verbatim_signals: Dict[str, Any] = Field(default_factory=dict)
    infra_flags: Dict[str, Any] = Field(default_factory=dict)
    recommended_actions: List[str] = Field(default_factory=list)
    truncated: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MergedFinding(BaseModel):
    incident_id: str
    primary_hypothesis: str
    merged_confidence: float = Field(..., ge=0.0, le=1.0)
    findings_by_agent: Dict[AgentRole, AgentFinding] = Field(default_factory=dict)
    ranked_findings: List[AgentFinding] = Field(default_factory=list)
    all_evidence: List[EvidenceItem] = Field(default_factory=list)
    verbatim_signals: Dict[str, Any] = Field(default_factory=dict)
    infra_flags: Dict[str, Any] = Field(default_factory=dict)
    corroboration_score: float = Field(default=0.0, ge=0.0, le=1.0)
    merged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

