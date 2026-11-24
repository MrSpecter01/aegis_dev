---
id: pm-2025-04-coredns
title: CoreDNS Failure Postmortem and Operational Rules
version: 1
types: [postmortem, rule]
tags: [network, dns, coredns, timeout, kubernetes]
symptoms: ["Intermittent DNS resolution timeout", "503 Service Unavailable", "upstream lookup failure"]
summary: Postmortem from historical DNS saturation event. Defines hard operational rules preventing concurrent node drains during DNS flapping.
---

## Symptom
Intermittent cross-service connection timeouts and 503 errors caused by DNS lookup failures across Kubernetes CoreDNS pods.

## Probable Causes
1. CoreDNS replicas were overwhelmed by sudden burst of un-cached external queries
2. Unhealthy node drain evacuated all CoreDNS pods simultaneously without upstream fallback

## Operational Rules
1. Never drain CoreDNS nodes simultaneously without verifying fallback upstream resolvers.
2. When DNS latency spikes occur, scale CoreDNS deployment first before restarting pods: `scale_deployment`.
3. Pod restarts must be rate-limited to avoid thundering herd on cluster DNS: `restart_pod`.

## Remediation Steps
1. Scale up CoreDNS deployment replicas: `scale_deployment`
2. Rate-limited restart of stuck DNS pods: `restart_pod`

## Escalation Criteria
- Upstream cloud provider DNS resolvers unreachable
- Latency persists after 3x horizontal scale
