# Triage Summary Template

Incident ID: {incident_id}
Severity: {severity}
Target Service: {service_name} (Namespace: {namespace})
Investigation Completed: {timestamp}

## Top Corroborated Hypotheses
{ranked_hypotheses}

## Key Telemetry & Evidence (Ranked)
{ranked_evidence}

## Infrastructure Flags
{infra_flags}

## Investigation Agent Status
- Infrastructure Investigator: {infra_status} (calls: {infra_tool_calls})
- Log & Trace Investigator: {logs_status} (calls: {logs_tool_calls})
- Change & Config Investigator: {change_status} (calls: {change_tool_calls})
