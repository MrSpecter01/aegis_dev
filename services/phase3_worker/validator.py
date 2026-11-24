"""Deterministic validation engine for Scribe incident reports.

Enforces structural completeness, grounding invariants, hypothesis framing,
and scorecard cleanliness without relying on an external LLM judge.
"""

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import ValidationError

from shared.models.enums import IncidentState
from shared.models.ledger import IncidentReport, LedgerEntry


REQUIRED_SECTIONS = [
    (r"^#\s+Incident Report\s*—", "Title (# Incident Report — <id>)"),
    (r"^##\s+Summary\b", "Summary (## Summary)"),
    (r"^##\s+Timeline\b", "Timeline (## Timeline)"),
    (r"^##\s+Investigation Findings\b", "Investigation Findings (## Investigation Findings)"),
    (r"^##\s+Actions Taken\b", "Actions Taken (## Actions Taken)"),
    (r"^##\s+Recovery Confirmation\b", "Recovery Confirmation (## Recovery Confirmation)"),
    (r"^##\s+Recommendations\b", "Recommendations (## Recommendations)"),
    (r"^##\s+Scorecard\b", "Scorecard (## Scorecard (to be filled by team))"),
]

SCORECARD_KPIS = [
    "Root Cause Accuracy",
    "Fix Effectiveness",
    "Resolution Latency",
    "Evidence Grounding",
    "Action Safety & Compliance",
    "Human Intervention Rate",
]

HYPOTHESIS_MARKERS = [
    "hypothes",
    "suspect",
    "likely",
    "probab",
    "potential",
    "preliminary",
    "indicat",
]


@dataclass
class ValidationResult:
    """Result of deterministic report validation."""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    validated_report: Optional[IncidentReport] = None

    @property
    def feedback_prompt(self) -> str:
        """Format error list as guidance for LLM reflection."""
        if not self.errors:
            return ""
        return "\n".join(f"- {err}" for err in self.errors)


def extract_section(markdown_text: str, section_title: str) -> str:
    """Extract content under a given ## section heading until the next ## heading."""
    lines = markdown_text.splitlines()
    in_section = False
    section_lines: List[str] = []
    target = section_title.lower()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            heading = stripped.lstrip("#").strip().lower()
            if target in heading:
                in_section = True
                section_lines.append(line)
                continue
            elif in_section:
                # Reached next ## section
                break
        elif in_section:
            section_lines.append(line)

    return "\n".join(section_lines).strip()


def validate_sections(markdown_text: str) -> List[str]:
    """Verify that all 7 required section headings are present."""
    errors: List[str] = []
    for pattern, name in REQUIRED_SECTIONS:
        if not re.search(pattern, markdown_text, flags=re.MULTILINE | re.IGNORECASE):
            errors.append(f"Missing required section: '{name}'.")
    return errors


def validate_scorecard(markdown_text: str) -> List[str]:
    """Verify scorecard contains all 6 KPIs and that all checkboxes are unfilled."""
    errors: List[str] = []
    scorecard_content = extract_section(markdown_text, "Scorecard")
    if not scorecard_content:
        # Already caught by validate_sections
        return errors

    # 1. Assert all 6 KPIs are present in the table
    for kpi in SCORECARD_KPIS:
        if kpi.lower() not in scorecard_content.lower():
            errors.append(f"Scorecard is missing required KPI: '{kpi}'.")

    # 2. Assert zero checkboxes are checked (anti-self-grading)
    # Catches [x], [X], [✓], [*], [v]
    if re.search(r"\[(?:\s*[xX✓*vV]\s*)\]", scorecard_content):
        errors.append(
            "Scorecard checkboxes must be completely UNFILLED. "
            "Do not evaluate the system yourself; use empty '[ ]' only."
        )

    return errors


def validate_root_cause_hypothesis(markdown_text: str) -> List[str]:
    """Verify that root cause in ## Summary is framed as a hypothesis rather than fact."""
    errors: List[str] = []
    summary_content = extract_section(markdown_text, "Summary")
    if not summary_content:
        summary_content = markdown_text

    root_cause_text: Optional[str] = None
    for line in summary_content.splitlines():
        line_clean = line.strip().lstrip("-*# ").strip()
        # Look for "Root Cause:" or "**Root Cause:**"
        lower_line = line_clean.lower()
        if lower_line.startswith("root cause") or lower_line.startswith("**root cause"):
            if ":" in line_clean:
                root_cause_text = line_clean.split(":", 1)[1].strip()
                # Strip any trailing bold markdown
                root_cause_text = root_cause_text.rstrip("*_").strip()
                break

    if not root_cause_text:
        errors.append("## Summary must contain a '- **Root Cause:**' line.")
        return errors

    if not root_cause_text or root_cause_text in ("[no record]", "___"):
        errors.append("Root cause is blank or marked unfilled.")
        return errors

    has_marker = any(marker in root_cause_text.lower() for marker in HYPOTHESIS_MARKERS)
    if not has_marker:
        errors.append(
            f"Root cause '{root_cause_text}' is not framed as a hypothesis and might be stated as an absolute fact. "
            "It must be explicitly framed as a hypothesis (e.g. 'Suspected...', 'Likely...', 'Hypothesized...')."
        )

    return errors


def validate_timeline_grounding(markdown_text: str, ledger_entries: List[LedgerEntry]) -> List[str]:
    """Verify timeline table entries reflect real ledger events with zero fabrication."""
    errors: List[str] = []
    timeline_content = extract_section(markdown_text, "Timeline")
    if not timeline_content:
        return errors

    # Set of valid ground truth event types and sources from ledger
    ground_truth_events: Set[str] = {
        entry.event_type.lower() for entry in ledger_entries if entry.event_type
    }
    ground_truth_states: Set[str] = {
        entry.state.value.lower() for entry in ledger_entries if entry.state
    }

    # Extract table rows
    table_lines = [line.strip() for line in timeline_content.splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 3:
        errors.append("Timeline must contain a Markdown table with at least one recorded event.")
        return errors

    # Skip header and divider rows
    data_rows = [row for row in table_lines[2:] if not re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", row)]

    for row in data_rows:
        cols = [col.strip() for col in row.strip("|").split("|")]
        if len(cols) < 3:
            continue
        
        # Check event or state mentions
        row_text = " ".join(cols).lower()
        if "[unknown" in row_text or "[no record]" in row_text:
            continue

        # Verify that at least one known event_type or state appears in this row
        row_has_known_event = any(ev in row_text for ev in ground_truth_events)
        row_has_known_state = any(st in row_text for st in ground_truth_states)

        if not (row_has_known_event or row_has_known_state):
            errors.append(
                f"Timeline table row '{row}' contains events or transitions not found in the incident ledger."
            )

    return errors


def validate_actions_and_denials(markdown_text: str, ledger_entries: List[LedgerEntry]) -> List[str]:
    """Verify that any denied or escalated actions are explicitly recorded in Actions Taken."""
    errors: List[str] = []
    actions_content = extract_section(markdown_text, "Actions Taken")
    if not actions_content:
        return errors

    # Detect if any ledger entry represents a denial
    has_denial = any(
        entry.event_type.lower() == "denied"
        or "denied" in str(entry.details).lower()
        for entry in ledger_entries
    )

    if has_denial and "denied" not in actions_content.lower():
        errors.append(
            "An action was denied during this incident, but ## Actions Taken does not mention the denial. "
            "All denied or escalated actions must be recorded."
        )

    return errors


def validate_incident_report_model(
    report_data: Optional[Dict[str, Any]],
    markdown_text: str,
) -> Tuple[Optional[IncidentReport], List[str]]:
    """Validate structured report data against IncidentReport Pydantic schema."""
    errors: List[str] = []
    if not report_data:
        errors.append("Missing structured IncidentReport payload.")
        return None, errors

    payload = dict(report_data)
    payload["markdown_content"] = markdown_text

    try:
        validated = IncidentReport.model_validate(payload)
        return validated, []
    except ValidationError as exc:
        for err in exc.errors():
            loc = " -> ".join(str(p) for p in err["loc"])
            errors.append(f"Pydantic schema validation error at '{loc}': {err['msg']}.")
        return None, errors


def validate_incident_report(
    markdown_text: str,
    ledger_entries: List[LedgerEntry],
    report_data: Optional[Dict[str, Any]] = None,
) -> ValidationResult:
    """Run all 6 deterministic validation checks over the Scribe output."""
    all_errors: List[str] = []

    all_errors.extend(validate_sections(markdown_text))
    all_errors.extend(validate_scorecard(markdown_text))
    all_errors.extend(validate_root_cause_hypothesis(markdown_text))
    all_errors.extend(validate_timeline_grounding(markdown_text, ledger_entries))
    all_errors.extend(validate_actions_and_denials(markdown_text, ledger_entries))

    validated_model, model_errors = validate_incident_report_model(report_data, markdown_text)
    all_errors.extend(model_errors)

    return ValidationResult(
        is_valid=(len(all_errors) == 0),
        errors=all_errors,
        validated_report=validated_model,
    )
