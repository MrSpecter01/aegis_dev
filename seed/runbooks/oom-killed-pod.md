---
id: oom-killed-pod
title: OOMKilled Pod Remediation
version: 1
types: [runbook, procedure]
tags: [memory, oom, pod, kubernetes]
symptoms: ["OOMKilled pods", "memory saturation above 90%"]
summary: Standard remediation procedure for Kubernetes pods terminated with OOMKilled status due to container memory saturation.
---

## Symptom
Pod(s) terminated with OOMKilled reason. Memory metrics show sustained pressure above 90% before kill.

## Probable Causes
1. Memory leak in application code — unbounded growth over time
2. Resource limits set too low for actual workload
3. Sudden traffic spike exceeding configured limits

## Investigation Steps
1. Check `k8s_get_events` for OOMKilled events in namespace
2. Review memory usage trend (last 30 min) in Prometheus
3. Check recent deployments — was a new version rolled out recently?

## Remediation Steps
1. Restart pod to clear immediate OOM state: `restart_pod`
2. If memory grows again rapidly → rollback to previous deployment version: `rollback_release`
3. If no recent deployment → increase memory limit: `update_resource_limits`

## Escalation Criteria
- Memory grows to OOM threshold again within 10 minutes of restart
- Multiple pods in same deployment affected simultaneously
- Database or stateful service is the OOMKilled component
