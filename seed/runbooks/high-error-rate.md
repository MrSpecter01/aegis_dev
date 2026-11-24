---
id: high-error-rate
title: Elevated HTTP 5xx Error Rate Remediation
version: 1
types: [runbook, procedure]
tags: [http, errors, 5xx, ingress, api]
symptoms: ["Elevated HTTP 5xx error rate", "failed requests exceeding SLO threshold"]
summary: Rapid response playbook for sudden spikes in HTTP 5xx server errors across API ingress and microservices.
---

## Symptom
Application error rate exceeds acceptable threshold (HTTP 5xx rate > 1%). Golden signals report elevated error ratios on ingress or service endpoints.

## Probable Causes
1. Defective release or bad configuration deployed recently
2. Downstream dependent service failing or timing out
3. Broken feature flag or external third-party API outage

## Investigation Steps
1. Check `loki_query` for recent 5xx error logs and exception stack traces
2. Check `argocd_get_sync_history` or `github_get_commits` for recent release changes
3. Check `tempo_search` for traces containing error tags to identify failing service hops

## Remediation Steps
1. If errors correlate with a recent deployment: `rollback_release`
2. If errors correlate with an active experimental feature: `toggle_feature_flag`
3. If errors stem from transient container failure or deadlock: `restart_pod`

## Escalation Criteria
- Error rate remains elevated after rollback or feature flag disablement
- Database or payment gateway unavailable
- Cascading failures impacting more than 3 downstream microservices
