---
id: node-pressure-eviction
title: Node Pressure and Eviction Remediation
version: 1
types: [runbook, procedure]
tags: [node, eviction, diskpressure, memorypressure, kubernetes]
symptoms: ["Pods evicted due to node disk pressure", "memory pressure", "PID pressure"]
summary: Cluster node condition recovery and pod rescheduling during disk, memory, or PID pressure evictions.
---

## Symptom
Kubernetes node condition reports `DiskPressure`, `MemoryPressure`, or `PIDPressure`. Pods on the node transition to `Evicted` status.

## Probable Causes
1. Unbounded log file growth or container ephemeral storage accumulation on the node
2. Node-level memory saturation starving system daemons
3. Colocated noisy neighbor pod consuming disproportionate host resources

## Investigation Steps
1. Call `k8s_get_node_metrics` to inspect host CPU, memory, and disk usage across cluster nodes
2. Check `k8s_get_events` for eviction events and node condition state changes
3. Call `k8s_get_pod_status` to identify failing or evicted pods on the affected node

## Remediation Steps
1. Drain degraded node safely to reschedule workloads onto healthy nodes: `drain_node`
2. If workload pods require redistribution or reduced pressure: `scale_deployment`
3. Update resource limits on runaway pods to enforce host cgroup boundaries: `update_resource_limits`

## Escalation Criteria
- More than 50% of cluster nodes reporting concurrent pressure conditions
- Critical control-plane components or DaemonSets failing to schedule
- Insufficient cluster capacity to accommodate evacuated workloads
