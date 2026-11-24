from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class RunbookCitation(BaseModel):
    runbook_id: str = ""
    book_name: str = ""
    section: str = "General"
    line_range: str = ""
    chunk_id: str = ""
    referenced_text: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"book_name": data}
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if not out.get("book_name"):
            out["book_name"] = str(
                out.get("title") or out.get("name") or out.get("book") or out.get("document") or ""
            ).strip()
        if not out.get("runbook_id"):
            out["runbook_id"] = str(
                out.get("id") or out.get("book_id") or out.get("doc_id") or ""
            ).strip()
        if not out.get("section"):
            out["section"] = str(out.get("section_name") or "General").strip()
        if not out.get("line_range"):
            raw_lines = out.get("lines") or out.get("line_numbers") or out.get("lines_range")
            if raw_lines:
                out["line_range"] = str(raw_lines).strip()
            elif out.get("line_start") is not None and out.get("line_end") is not None:
                out["line_range"] = f"L{out['line_start']}-L{out['line_end']}"
        if not out.get("chunk_id"):
            raw_chunk = out.get("chunk") or out.get("chunk_label")
            if raw_chunk:
                out["chunk_id"] = str(raw_chunk).strip()
            elif out.get("chunk_index") is not None:
                if out.get("total_chunks") is not None:
                    out["chunk_id"] = f"chunk {out['chunk_index']}/{out['total_chunks']}"
                else:
                    out["chunk_id"] = f"chunk {out['chunk_index']}"
        if not out.get("referenced_text"):
            out["referenced_text"] = str(
                out.get("excerpt")
                or out.get("quote")
                or out.get("content")
                or out.get("reference")
                or out.get("data")
                or out.get("text")
                or ""
            ).strip()
        return out


class ActionPlanItem(BaseModel):
    tool_name: str
    target_resource: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    rationale: str
    pre_approved: bool = False
    runbook_citations: List[RunbookCitation] = Field(default_factory=list)


class ActionProposal(BaseModel):
    incident_id: str
    actions: List[ActionPlanItem] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    risk_assessment: str
    rationale: Optional[str] = None
    runbook_citations: List[RunbookCitation] = Field(default_factory=list)
    proposed_by: str = "remediation_agent"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalRequest(BaseModel):
    request_id: str
    incident_id: str
    proposal: ActionProposal
    channel_notified: List[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalDecision(BaseModel):
    incident_id: str
    approved: bool
    decided_by: str
    channel: str  # telegram, dashboard, etc.
    reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

