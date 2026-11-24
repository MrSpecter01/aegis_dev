---
id: crash-loop-backoff
title: Container CrashLoopBackOff Remediation
version: 1
types: [runbook, procedure]
tags: [crashloop, pod, container, exitcode, kubernetes]
symptoms: ["Pod in CrashLoopBackOff", "container exiting with non-zero exit code immediately on startup"]
summary: Diagnostic and recovery procedure for containers failing startup checks or terminating prematurely in CrashLoopBackOff.
---

## Symptom
Pod status oscillates between `CrashLoopBackOff` and `Error`. Restart count increments continuously and container fails health/liveness probes.

## Probable Causes
1. Missing environment variable, corrupt ConfigMap, or invalid secret reference
2. Fatal runtime exception during application initialization
3. Incompatible binary or bad container image deployed in latest release

## Investigation Steps
1. Call `k8s_get_events` to retrieve container exit codes, termination messages, and restart counts
2. Check `loki_query` for standard error output and startup panic messages
3. Check `k8s_get_configmap_diff` or `argocd_get_sync_history` for recent configuration alterations

## Remediation Steps
1. If the crash started following a deployment or config change: `rollback_release`
2. If temporary filesystem or external locking state caused the crash: `restart_pod`
3. If memory or CPU is terminating initialization sequence prematurely: `update_resource_limits`

## Escalation Criteria
- Container fails across all replicas with configuration syntax errors requiring git commit
- Rollback release fails to stabilize application startup
- Image pull failure or missing credentials from external registry
