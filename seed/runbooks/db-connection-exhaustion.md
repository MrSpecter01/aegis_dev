---
id: db-connection-exhaustion
title: Database Connection Pool Exhaustion Remediation
version: 1
types: [runbook, procedure]
tags: [database, connections, timeout, postgresql, redis]
symptoms: ["Database connection pool exhausted", "queries timing out", "client connection errors"]
summary: Mitigation procedures for saturated relational database or cache connection pools causing client query timeouts.
---

## Symptom
Application logs show database connection pool timeouts (e.g. `too many connections`, `pool exhausted`, `connection refused`). Backend services hang waiting for database handles.

## Probable Causes
1. Sudden traffic surge scaling frontend pods beyond database connection capacity
2. Leaked database connections due to unclosed sessions in application code
3. Long-running or unindexed analytical queries locking connection handles

## Investigation Steps
1. Query Prometheus for database active connection count vs max connection limit
2. Check `loki_query` for database driver errors and unhandled connection timeouts
3. Review recent scaling actions or traffic spikes in `k8s_get_events`

## Remediation Steps
1. Scale down non-critical application replicas to release connections: `scale_deployment`
2. If memory or CPU on connection proxy or stateful pool is saturated: `update_resource_limits`
3. Restart stuck application pods holding dangling leaked handles: `restart_pod`

## Escalation Criteria
- Primary database unresponsive or crashing
- Replicas fail to recover connections after client pod restarts
- Data corruption or replication lag exceeds recovery window
