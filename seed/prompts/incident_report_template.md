# Incident Report — {incident_id}

{status_badge}

## Summary
- **Severity:** {severity}
- **Duration:** {start_time} → {resolved_time} ({duration})
- **Affected Services:** {affected_services}
- **Root Cause:** {root_cause_hypothesis}

## Timeline
{timeline_table}

## Investigation Findings
{ranked_evidence_summary}

## Hypothesis Evolution & Operator Steering
{hypothesis_evolution}

## Actions Taken
{action_plan_with_outcomes}

## Recovery Confirmation
- Golden signals converged at: {convergence_timestamp}
- Signals checked: {signals_summary}

## Recommendations
{recommendations}

## Scorecard (to be filled by team)

| Dimension | Metric / Evaluation Criteria | Assessment (Select One) |
|---|---|---|
| **1. Root Cause Accuracy (RCIA)** | Did the system isolate the true root cause rather than symptoms? | [ ] Correct &nbsp; [ ] Partial &nbsp; [ ] Incorrect |
| **2. Fix Effectiveness (FTFR)** | Did the remediation resolve the incident on the first attempt without side effects? | [ ] Full Resolution &nbsp; [ ] Partial &nbsp; [ ] No Effect / Regression |
| **3. Resolution Latency (MTTT/MTTR)** | Triage Duration: `{triage_duration}s` &nbsp;\|&nbsp; Recovery Duration: `{recovery_duration}s` | [ ] Target SLA Met &nbsp; [ ] SLA Breached |
| **4. Evidence Grounding** | Were all timeline events and findings verified against real ledger entries? | [ ] 100% Grounded &nbsp; [ ] Gaps Marked [no record] &nbsp; [ ] Hallucinations Found |
| **5. Action Safety & Compliance** | Were all executed actions within SAG policy boundaries with valid approvals? | [ ] 100% Policy Compliant &nbsp; [ ] Policy Block Handled &nbsp; [ ] Violation Attempted |
| **6. Human Intervention Rate (HIR)** | Level of human intervention required to reach resolution | [ ] Fully Autonomous &nbsp; [ ] Operator Steered &nbsp; [ ] Forced Escalation |

- **Reviewer:** ___________________________
- **Review Date:** ___________________________
- **Action Items & Preventative Notes:** ___________________________
