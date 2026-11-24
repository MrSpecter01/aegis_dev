# Remediation Reasoning Template

## Context & Signals
- Incident ID: {incident_id}
- Target Service: {service_name}
- Target Namespace: {namespace}
- Severity: {severity}
- Primary Root Cause Hypothesis: {primary_hypothesis}
- Corroborated Evidence:
{evidence_summary}

## Knowledge Base & Operational Rules
- Active Operational Rules:
{operational_rules}
- Relevant Runbook Entries:
{runbook_citations}

## Prior Attempts (if retry loop > 1)
{prior_attempts_history}

## Reasoning Objective
Formulate an evidence-backed, minimal-blast-radius ActionProposal. Ensure the chosen action is explicitly allowed by the matching runbook and SAG policy, does not repeat failed prior attempts, and adheres to all active operational rules.
