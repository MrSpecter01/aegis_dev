---
id: network-latency
title: Inter-Service Network Latency Remediation
version: 1
types: [runbook, procedure]
tags: [latency, network, p99, timeout, rpc]
symptoms: ["Elevated p99 inter-service latency", "cross-service RPC timeouts", "packet drop"]
summary: Diagnostic and mitigation actions when inter-service RPC latency or network transport delays breach SLO limits.
---

## Symptom
Golden signal p99 latency spikes significantly above normal baseline (> 500ms). Downstream services encounter RPC connection timeouts and deadline exceeded errors.

## Probable Causes
1. Cross-service network congestion or faulty service mesh sidecar proxy
2. Single slow pod instance degrading load-balanced upstream requests
3. Backend service CPU throttling causing delayed request dispatch

## Investigation Steps
1. Call `tempo_search` and `tempo_get_trace` to identify the latency breakdown along the call graph
2. Query Prometheus for p99 latency metrics grouped by service and pod instance
3. Check `k8s_get_pod_status` and CPU throttling metrics for pods on the critical path

## Remediation Steps
1. Scale up healthy backend replicas to absorb throughput and distribute load: `scale_deployment`
2. If latency is caused by a bad release or unindexed route: `rollback_release`
3. Restart degraded or unresponsive single pod instances: `restart_pod`

## Escalation Criteria
- Latency spike affects inter-cluster ingress or transit routing
- P99 latency remains degraded after horizontal replica scaling
- Cascading timeouts propagate across all tier-1 user-facing flows
