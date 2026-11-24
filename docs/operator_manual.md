# Aegis — Operator Manual & SRE Playbook

> **Audience:** Site Reliability Engineers (SREs), platform engineers, and incident commanders configuring, customizing, and operating Aegis in production or staging environments.  
> **Scope:** Production onboarding, incident lifecycle & multi-tier convergence, Redis/disk state management, Human-in-the-Loop (HITL) controls, Safety Action Gateway (SAG) default-deny policies, deep customization guides, security hygiene, and Day-2 CLI operations.

---

## Table of Contents

- [1. Production Setup & Target System Onboarding](#1-production-setup--target-system-onboarding)
- [2. Incident Lifecycle, Multi-Tier Convergence & Data Retention](#2-incident-lifecycle-multi-tier-convergence--data-retention)
- [3. Human-in-the-Loop (HITL) & Emergency Controls](#3-human-in-the-loop-hitl--emergency-controls)
- [4. SRE Knowledge Management, Runbooks & Postmortem Customization](#4-sre-knowledge-management-runbooks--postmortem-customization)
- [5. Safety Action Gateway (SAG), Tooling & Default-Deny Access Boundaries](#5-safety-action-gateway-sag-tooling--default-deny-access-boundaries)
- [6. Multi-Channel Operations, Security Hygiene & Day-2 CLI](#6-multi-channel-operations-security-hygiene--day-2-cli)

---

## 1. Production Setup & Target System Onboarding

While the quick trial uses pre-seeded sample runbooks, prompts, and sample topology charts, deploying Aegis against a live target environment requires aligning your secrets, observability endpoints, safety boundaries, and topology graph.

### 1.1 Environment Secrets & Credentials (`deploy/.env`)

Copy `deploy/.env.example` to `deploy/.env` and `config/identities.yaml.example` to `config/identities.yaml`, then configure all credentials and personal identifiers strictly in `.env` (never in YAML files):

- **Control Plane & Multi-Entity Auth (`config/identities.yaml` & `deploy/.env`):** Define RBAC roles and scopes for human operators, alert webhooks, and external API clients in `config/identities.yaml` referencing environment variables stored strictly in `deploy/.env` (such as `AEGIS_API_KEY`, `AEGIS_ADMIN_TELEGRAM_ID`, `AEGIS_ALERTMANAGER_API_KEY`, and per-operator `AEGIS_OPERATOR_*_KEY` / `AEGIS_OPERATOR_*_TELEGRAM_ID` values). `deploy/docker-compose.yml` mounts `deploy/.env` (and optional root `.env`) into `aegis-api` via `env_file`, so custom env-referenced identities activate automatically upon container start/recreate. Entries activate only when their referenced env var is populated. External API port `8080` enforces scoped API keys on every call, while internal Aegis services communicate over the Docker-only port `8081` using `X-Aegis-Service` tracking without requiring external API keys.
- **LLM & Embedding Access:** Set `GOOGLE_API_KEY`, `OPENAI_API_KEY`, or `OLLAMA_URL` (`http://host.docker.internal:11434` when using host Ollama, or `http://ollama:11434` with `--profile ollama`).
- **Observability Targets & Outbound Auth:**
  - If using your **external** observability stack, leave `COMPOSE_PROFILES=""` (empty) in `deploy/.env` and set `PROMETHEUS_URL`, `LOKI_URL`, and `TEMPO_URL` to your cluster/monitoring URLs, plus optional outbound bearer/API-key env vars (`PROMETHEUS_API_KEY`, `LOKI_API_KEY`, `TEMPO_API_KEY`, `JAEGER_API_KEY`, `FLAGD_API_KEY`).
  - If using Aegis's **bundled** LGTM stack, set `COMPOSE_PROFILES=monitoring` and configure `deploy/prometheus/prometheus.yml` to scrape your target endpoints.
- **Target Mutation & GitOps Credentials:** Configure `KUBECONFIG_PATH` (mounted via `./:/app/deploy:ro`), `KUBERNETES_URL` / `KUBERNETES_TOKEN`, `ARGOCD_URL` / `ARGOCD_TOKEN`, and `GITHUB_TOKEN` as required by your active tools.
- **Telegram HITL *(Optional)*:** Set `TELEGRAM_BOT_TOKEN` and the authorized `TELEGRAM_CHAT_ID`.

### 1.2 Step-by-Step Target Onboarding Checklist

1. **Wire Observability & Read Tools (`config/system.yaml`, `config/mcp.yaml`):**
   Update `mcp_endpoints` and `convergence.queries` in `config/system.yaml` so PromQL golden-signal expressions match the metric names and labels emitted by your target services, and verify all MCP server entries in `config/mcp.yaml`.
2. **Configure Safety Action Gateway Boundaries (`config/sag_policies.yaml`):**
   Update `allowed_namespaces` (top-level and per-tool) to list only your target namespaces. Review `max_replicas`, `max_cpu_increase_ratio`, `max_memory_increase_ratio`, `cooldowns`, `confidence_thresholds`, and `approval_matrix` timeouts.
3. **Configure Multi-Entity Identities & RBAC (`config/identities.yaml`, *optional*):**
   Copy `config/identities.yaml.example` to `config/identities.yaml` to register per-operator roles (`admin`, `operator`, `viewer`), Alertmanager webhooks (`alerts:ingest`), and external clients via `api_key_env` and `telegram_user_id_env` references pointing to `deploy/.env` (see [Section 5.4](#54-multi-entity-rbac--identity-registry-configidentitiesyaml)).
4. **Route Firing Alerts to Aegis:**
   Configure Alertmanager (either your existing cluster Alertmanager or `deploy/alertmanager/alertmanager.yml`) to dispatch firing alert webhooks to `POST http://<aegis-host>:8080/v1/alerts/ingest` with the `X-API-Key` header. Ensure alert labels include `service`, `namespace`, and `severity`.
5. **Author Domain Runbooks & Invariants (`seed/runbooks/`):**
   Replace or supplement sample runbooks with Markdown runbooks, operational rules, and known postmortems tailored to your architecture (use dedicated Qdrant collections per unrelated application; see [Section 4.3](#43-isolating-unrelated-applications-with-dedicated-qdrant-collections)).
6. **Start the Stack & Initialize via `aegis-api`:**
   ```bash
   docker compose -f "deploy/docker-compose.yml" build aegis-base
   docker compose -f "deploy/docker-compose.yml" up -d --build
   # Wait until `docker ps` shows aegis-api, aegis-redis, aegis-qdrant, and aegis-mcp as (healthy) before running exec commands (first build/boot can take a few minutes)

   docker exec aegis-api aegis init --helm-dir /app/charts/<your-chart> --namespace <target-ns>
   # Expected: topology nodes/edges initialized and SHA-256 digest saved

   docker exec aegis-api aegis seed runbooks --strict
   # Expected: rule lint passes and Qdrant chunks upsert (first-run embedding may take ~15-60s)

   docker exec aegis-api aegis seed prompts
   # Expected: prompt templates validated cleanly

   docker exec aegis-api aegis verify
   # Expected: Redis, Qdrant, LLM/Ollama, and MCP servers report healthy/OK

   curl -sf -H "X-API-Key: <AEGIS_API_KEY>" http://localhost:8080/v1/auth/me
   # Expected: HTTP 200 with your operator JSON (if 403, confirm deploy/.env key matches config/identities.yaml and recreate aegis-api)
   ```

---

## 2. Incident Lifecycle, Multi-Tier Convergence & Data Retention

Aegis operates as an autonomous first-responder that correlates firing alerts, runs parallel root-cause investigations, proposes policy-bounded fixes for human sign-off, verifies multi-tier recovery, and compiles a ledger-grounded postmortem report.

### 2.1 Alert Detection, Deduplication & Correlation

- **Alert Ingestion, Namespace Scope & Deduplication:** Incoming Alertmanager webhooks (`POST /v1/alerts/ingest`) validate payloads against Pydantic schemas, **drop out-of-scope namespaces** not listed in `ingestion.allowed_namespaces` / `topology.monitored_namespaces` (recording dropped alerts in `stream:alerts:dropped`, `stream:audit:interactions`, and `GET /v1/alerts/dropped`), and group repeated alerts by fingerprint (`fingerprint_ttl_seconds`, default `3600s` in `config/system.yaml`). Re-firing alerts merge into the open incident instead of spawning duplicates.
- **Topological & Temporal Correlation:** Alerts firing within `correlation_window_seconds` (default `120s`) across services connected in the topology dependency graph are merged into a single multi-service incident (`affected_services`).
- **Flap Suppression:** If the same fingerprint fires more than `flap_threshold` times (default `6` in `config/system.yaml`) within `flap_window_seconds` (default `60s`), Aegis marks the stream as flapping and suppresses duplicate incident creation.
- **Severity Assignment:** Severity maps from the incoming alert's `severity` label (`critical` → `P1`, `warning` → `P2`, `info` → `P3`; defaults to `P3` when absent), controlling retry budgets, confidence thresholds, and approval timeouts.

#### To Customize Alert Ingestion & Correlation
Edit the `ingestion:` block in `config/system.yaml`:
- Set `ingestion.allowed_namespaces` to override which namespaces are accepted at ingest and correlation (when omitted/empty, Aegis falls back to `topology.monitored_namespaces`); out-of-scope alerts are dropped at both API ingest and correlator stages, logged in Redis (`stream:alerts:dropped` and `stream:audit:interactions`), and queryable via `GET /v1/alerts/dropped`.
- Adjust `correlation_window_seconds`, `flap_window_seconds`, `flap_threshold`, and `fingerprint_ttl_seconds`.
- Map custom alert severity label values inside `ingestion.severity_map` (for example mapping `page: P1` or `high: P1`).

### 2.2 End-to-End Incident State Machine & Phase Workflow

Every incident advances through an 11-state finite state machine (`services/api_server/state_machine.py`) with every transition appended to the immutable Redis ledger:

```text
NEW → GROUPING → TRIAGE → MERGING → REMEDIATION → AWAITING_APPROVAL → EXECUTING → MONITORING → REPORTING → CLOSED
                                          │              │                 │            │
                                          └──────────────┴─────────────────┴────────────┴──→ ESCALATED (or STANDBY)
```

1. **Triage & Merging (`TRIAGE` → `MERGING`, `phase1-worker`):**
   Three CrewAI investigator agents execute concurrently using read-only telemetry:
   - **`infrastructure_investigator`:** Inspects pod statuses, container exit codes (`OOMKilled` / exit `137`, `CrashLoopBackOff`), cluster warning events, and node/container metrics.
   - **`log_trace_investigator`:** Queries application logs and distributed traces (Tempo/Jaeger) to trace upstream vs. downstream errors.
   - **`change_config_investigator`:** Examines Git commits, ArgoCD sync history, and Kubernetes ConfigMap diffs within the incident window.
   - Findings are deterministically merged—preserving exact signal strings and metric values verbatim—and ranked by cross-agent corroboration.
2. **Remediation Planning (`REMEDIATION`, `phase2-worker`):**
   The LangGraph remediation workflow always direct-injects `<operational_rules>` (`type: rule`), delivers the runbook catalog according to `qdrant.catalog_mode` (`auto` inlines `<runbook_catalog>` up to `qdrant.catalog_inline_max_entries: 15`, switching to 2-step `search_runbook_catalog` → `qdrant_search` retrieval above the threshold; `direct` and `two_step` force either mode), and formulates a structured `ActionProposal` with a confidence score (`0.0–1.0`), separated rationale and multi-line runbook reference (`id/title`, `section/line_range`, reference excerpt), target parameters, and cited runbooks.
3. **Approval Gate (`AWAITING_APPROVAL`):**
   Unless every proposed tool is marked `pre_approved: true` (and permitted by `approval_matrix`), Aegis pauses and dispatches an interactive approval request to both the Dashboard and Telegram.
4. **Execution & Verification (`EXECUTING` → `MONITORING`):**
   Executes the approved action through the two-layer Safety Action Gateway (SAG), runs multi-tier convergence monitoring, and appends observed convergence telemetry/verdicts directly into the immutable ledger (`signals_converged` / `signals_not_converged`).
5. **Postmortem Reporting (`REPORTING` → `CLOSED`, `phase3-worker`):**
   Scribe synthesizes `report.md` from the append-only ledger (including observed **Recovery Confirmation** metrics/tier checks and any operator-entered action reasons across Dashboard or Telegram), persists incident artifacts to disk, and dispatches the final report.

### 2.3 Multi-Tier Convergence & Recovery Verification

Aegis does **not** rely solely on a single metric check to declare an incident resolved. Recovery is verified across five sequential tiers (`services/phase2_worker/graph.py` & `services/phase2_worker/convergence.py`):

| Tier | Stage | What Is Checked & Observable Behavior |
| --- | --- | --- |
| **Tier 0** | Pre-Execution Live Substrate Gate (`EXECUTING`, prior to write tool) | Queries `k8s_get_pod_diagnostics` for the target workload right before executing any cluster mutation. **Self-Resolved (`RESOLVED_SKIP`):** if pods are already healthy, skips disruptive writes and jumps to verification. **Condition Mutated (`MUTATION_DETECTED_REPLAN`):** if the failure mode changed after approval (e.g., scaled for CPU, but pod is now `ImagePullBackOff`), aborts the incompatible tool and returns to `REMEDIATION` to re-plan. |
| **Tier 1** | Workload-Aware Stabilization Bake Time | Waits before sampling so pods/caches settle (`bake_time_seconds_stateless`, default `20s`; `bake_time_seconds_stateful`, default `40s` when service matches `db`, `mongo`, `mysql`, `redis`, `postgres`, `rabbit`, or `queue`). |
| **Tier 2** | Golden Signal PromQL SLO Polling | Polls Prometheus via MCP every `poll_interval_seconds` (default `10s`) across all `affected_services` against four configured SLOs: `error_rate` (`≤ 0.01`), `p99_latency_ms` (`≤ 500 ms`), `cpu_saturation` (`≤ 0.85`), and `memory_saturation` (`≤ 0.90`). |
| **Tier 3** | Live Kubernetes Pod & Container Readiness | When `affected_namespace` is set, calls `k8s_get_pod_status` on matching workload pods and requires `phase == "Running"`, `ready == True`, and zero containers reporting `CrashLoopBackOff`, `OOMKilled`, or `Error` (in either `waiting` or `terminated` status). |
| **Tier 4** | Consecutive Clean-Sample Window & Fail-Closed Guard | Any SLO breach, unhealthy pod, or `None` / missing / non-finite (`NaN` or `Inf`) metric resets `consecutive_clean_samples` to `0`. Safe checkpoints before bake time, after bake time, before every PromQL cycle, and before Tier 3 Kubernetes pod checks immediately hold off read calls and freeze the evaluation `deadline` during System Halt / `STANDBY` (or abort on `Drop Issue`). Aegis transitions to `REPORTING` only after passing `window_seconds / poll_interval_seconds` consecutive clean samples (default `6`). Both converged and timed-out convergence metrics/tier results are written into the append-only ledger (`signals_converged` / `signals_not_converged`) for Phase 3 **Recovery Confirmation** reporting and `prior_attempts` re-planning context (up to `max_retries`, or `ESCALATED`). |

#### To Customize Convergence & Golden Signals
Edit the `convergence:` section in `config/system.yaml` (and matching retry/confidence limits in `config/sag_policies.yaml`):
- Tune `bake_time_seconds_stateless`, `bake_time_seconds_stateful`, `poll_interval_seconds`, and `window_seconds`.
- Adjust SLO bounds under `convergence.slo` (`error_rate_threshold`, `p99_latency_ms`, `cpu_saturation_threshold`, `memory_saturation_threshold`).
- Customize PromQL templates under `convergence.queries` (`{service}` is substituted per affected service) to match custom Prometheus job/pod labels or service-mesh metrics (e.g., Istio/Linkerd request metrics).

### 2.4 Per-Incident Granular Data in Redis & Disk Archival Lifecycle

While an incident is active, all granular operational telemetry, state, and audit records are stored in Redis per incident for fast querying by workers, API endpoints, and deep live inspection:

- **Per-Incident Redis Keys (`incident:<incident_id>:*`):**
  - `:state` — Current FSM state, severity, `visited_states`, timestamps, `affected_service`, `affected_namespace`, `retry_count`.
  - `:ledger` — Append-only Redis list (`RPUSH`) of serialized `LedgerEntry` events covering every transition, diagnostic snapshot, and action.
  - `:context` — Merged Phase 1 root-cause hypothesis, confidence score, corroboration score, ranked evidence, and `affected_services`.
  - `:flags` — Platform/symptom flags emitted by Phase 1 investigators (`oom_killed`, etc.).
  - `:alert_group` — Correlated alert metadata and active fingerprints (fingerprints are cleared when the incident enters `CLOSED`).
  - `:action` & `:action:<action_id>` — Current proposal payload, approval status (`pending`, `approved`, `denied`, `steered`, `cancelled`), actor, channel, and timestamps.
  - `:proposal`, `:prior_attempts`, `:mutation_delta`, `:steering` — Remediation iterations, signals recorded at convergence timeouts, pre-execution mutation deltas, and operator guidance history.
  - `:approved_actions`, `:pre_pause_state`, `:dropped`, `:dumped_at` — Control flags for authorization verification, standby restoration, aborted processing, and archival tracking.
- **Disk Archival (`dump_on_close` & Idle Dump Window `dump_idle_seconds`):**
  Aegis separates **when data is written to disk** from **when Redis reclaims RAM**:
  - When Phase 3 delivers a report (`dump_on_close: true`), when an incident transitions to `CLOSED`, **or** when any inactive incident remains idle in Redis longer than **`ledger.dump_idle_seconds`** (default `1800s` / `30 minutes`), Aegis flushes the incident to persistent disk (`ledger.storage_path`, default `/data/incidents/<incident_id>/`):
    1. `ledger_full.json` — Complete JSON export of all append-only ledger entries.
    2. `incident_snapshot.json` — Complete snapshot of all granular Redis keys (`:state`, `:context`, `:flags`, `:action`, `:prior_attempts`, `:steering`, etc.).
    3. `report.md` — Final postmortem Markdown report (written once Phase 3 Scribe completes).
    4. `topology_snapshot.json` — Point-in-time dependency topology snapshot captured for the incident.
- **Redis RAM Expiration (`redis_ttl_seconds`):**
  Separately from disk dumping, **`ledger.redis_ttl_seconds`** (default `86400s` / `24 hours`) controls how long Redis keeps incident keys in memory after closure or idle disk archival before calling `EXPIRE` via `set_incident_ttl()` (`services/api_server/incident_store.py`) to free expensive running RAM.

#### To Customize Redis Disk-Dump Idle Time & RAM TTL
Configure under `ledger:` in `config/system.yaml` (or via `AEGIS_DUMP_IDLE_SECONDS` / `AEGIS_REDIS_TTL_SECONDS` environment variables):
- `dump_on_close`: Set `true` (default) to persist artifacts immediately when an incident reaches `CLOSED`.
- `dump_idle_seconds`: Idle window in seconds before background archiver sweeps flush inactive Redis incident state + ledger entries to disk (set `0` to disable idle sweeps and dump only on close).
- `redis_ttl_seconds`: Expiration TTL in seconds applied to all `incident:<id>:*` Redis keys after closure or idle archival so Redis memory stays bounded while disk copies remain permanent (set `0` to disable automatic Redis key expiration).
- `storage_path`: Target disk directory for archived incident folders (default `/data/incidents`).

---

## 3. Human-in-the-Loop (HITL) & Emergency Controls

On-call SREs retain final authority over cluster mutations and lifecycle state at every stage.

### 3.1 Reviewing an Action Proposal

When an incident pauses at `AWAITING_APPROVAL`, inspect the proposal in Telegram or the Dashboard **Approval Queue**:
- **Root-Cause Hypothesis & Confidence Score:** Corroborated telemetry cited by Phase 1 & Phase 2.
- **Tool Call & Exact Parameters:** Target namespace, deployment/pod name, replica count, resource limits, or feature flag name/environment.
- **Reversibility & Blast Radius:** Cross-reference upstream/downstream dependencies on the **Topology** tab.
- **Runbook Citations:** Structured multi-line references (`id/title`, `section/line_range`, excerpt) separated cleanly from the proposal rationale across Dashboard and Telegram.

### 3.2 Three-Way Approval Decisions: Approve, Deny, and Steer

| Decision | Observable Behavior | When to Use |
| --- | --- | --- |
| **Approve** | Authorizes the specific `action_id` and its bound tool list, advancing the incident to `EXECUTING`. Duplicate approvals return `409 Already decided`. | The diagnosis, target resource, and parameters are accurate and safe. |
| **Deny** | **Deny Wins unconditionally.** Immediately blocks execution, transitions to `ESCALATED`, and updates the opposite channel (a Dashboard denial edits/invalidates open Telegram buttons). Even a late denial *after* approval—while in `EXECUTING`—evicts approval and escalates immediately. | The action is unsafe, targets the wrong component, or requires manual remediation. |
| **Steer** | Stores operator guidance, notifies both channels, and loops back to `REMEDIATION` so Phase 2 incorporates your instruction into a fresh proposal. | Diagnosis is close, but you want a different action (e.g., *"Scale `frontend` to 3 replicas instead of restarting"*). |

- **Mid-Flight Steering:** Sending steering instructions while Phase 2 is actively reasoning injects `[OPERATOR MID-FLIGHT GUIDANCE]` directly between agent turns.
- **Operator Reason Audit Trail:** Any reason or comment entered during Approve, Deny, Steer, Pause, Escalate, or Close (from either Dashboard or Telegram) is recorded in the immutable ledger alongside the authenticated operator username (`actor` / `decided_by`) and surfaced in the postmortem `report.md`.
- **Re-Approval After Denial:** If you initially denied an action (`ESCALATED`) and later decide to proceed, issuing an explicit approval for that `action_id` re-authorizes it (`ESCALATED → EXECUTING`).

### 3.3 Incident Lifecycle Controls

- **Pause / Standby & Resume:** Moves an active incident into `STANDBY`, halting automated progression. Resuming without new text restores the exact `pre_pause_state` (`TRIAGE`, `AWAITING_APPROVAL`, `EXECUTING`, or `MONITORING`); resuming with steering guidance routes directly to `REMEDIATION`.
- **Force Escalate:** Transitions any active incident immediately to `ESCALATED`.
- **Drop Issue:** Sets `incident:<id>:dropped`, cancels active proposals (`pending`/`approved` → `cancelled`), clears Redis stream processing, logs a `drop` event, and closes (`CLOSED`) without generating a postmortem report.
- **Manual Close:** Closes an active or escalated incident immediately, optionally triggering Phase 3 report generation (`generate_report: true`).

### 3.4 Fleet-Wide System Halt: Immediate Write Block & Safe-Checkpoint Pause

Available via the **System Halt** toggle in the Dashboard header (`POST /v1/system/pause`), System Halt acts as an immediate fleet-wide emergency brake designed to stop all cluster mutations immediately while pausing workers safely at clean checkpoints:

1. **Immediate Write-Tool Cutoff:** Before any write tool reaches an MCP server, `SAGGuard` checks `system:status:paused` in Redis (`shared/harness/sag_guard.py`) and immediately blocks execution (`SAGActionBlockedError`), recording the block in the incident ledger. Zero cluster mutations can occur while System Halt is active.
2. **Immediate API Mutation Lock:** All mutating incident endpoints (`/approve`, `/steer`, `/escalate`, `/pause`, `/resume`, etc.) immediately fail closed with `HTTP 423 Locked` (`_verify_not_emergency_halt`).
3. **Safe-Checkpoint Worker Suspension:** All worker consumer loops (`phase1-worker`, `phase2-worker`, `phase3-worker`) poll `system:status:paused` before pulling work from `stream:phase1:jobs`, `stream:phase2:jobs`, and `stream:phase3:jobs`.
   - Active I/O steps already finishing (such as pushing an alert notification, appending a ledger entry, or finishing a `report.md` write) complete cleanly at their checkpoint so no file or ledger state is corrupted mid-write.
   - **Mid-Convergence Safe Checkpoints:** Even if an incident is already inside post-remediation `MONITORING`, `run_convergence_monitor` checks `system:status:paused`, incident `STANDBY`, and `incident:<id>:dropped` before/after bake time, before every PromQL cycle, and before Kubernetes pod readiness checks—holding off read-tool calls and freezing the convergence deadline until resumed (or aborting on drop).
   - Queued jobs—including new triage read-tool runs, Phase 2 remediation loops, and Phase 3 deliveries—remain safely queued in Redis streams without starting until System Halt is lifted (`POST /v1/system/resume`).

### 3.5 To Customize Operator Notification & Approval Channels

Aegis separates control-plane state transitions (`services/api_server/state_machine.py`) from notification/approval delivery so you can add Slack, PagerDuty, Microsoft Teams, or custom webhook channels alongside (or instead of) Telegram and the Web UI:

- **Lifecycle Notifications:** `services/api_server/notifications.py` (`dispatch_notification`) posts `NotificationPayload` JSON (`incident_created`, `triage_summary`, `approval_request`, `denial_notification`, `escalation`, `report_delivered`, `drift_detected`) to `POST /notify`. Add a companion webhook/adapter service listening on the same payload format, or invoke an additional channel client inside `dispatch_notification()`.
- **Interactive Approval & Steering:** Have your new channel adapter call the existing Control Plane REST endpoints:
  - `POST /v1/incidents/{id}/approve` (`decision`: `"approved"`, `"denied"`, or `"steer"`)
  - `POST /v1/incidents/{id}/steer`
  - `POST /v1/system/pause` / `POST /v1/system/resume`
- **Custom LangGraph Workflow Nodes:** Because Phase 2 (`services/phase2_worker/graph.py`) and Phase 3 (`services/phase3_worker/graph.py`) are built on LangGraph `StateGraph`, you can also insert custom approval/notification nodes (for example, paging an external ticket system inside `request_approval_node` or exporting reports to Confluence/S3 inside `deliver_report_node`) by extending the graph definition and wiring it into the worker.

---

## 4. SRE Knowledge Management, Runbooks & Postmortem Customization

Aegis uses a **Hybrid RAG 2.0** architecture backed by Qdrant and a compiled Markdown catalog (`seed/runbooks_index.json`).

### 4.1 How Phase 2 Remediation Uses Runbooks & Rules

Phase 1 investigators focus strictly on raw telemetry (`metrics`, `logs`, `traces`, `k8s`, `git`), while **Phase 2 (`REMEDIATION`)** consumes operational knowledge during fix formulation (`services/phase2_worker/graph.py`):

Every Markdown file in `seed/runbooks/` starts with YAML frontmatter defining its `id`, `title`, `version`, `types`, `tags`, `symptoms`, and `summary`:

| Classification (`types`) | How Phase 2 Uses It | Recommended Content |
| --- | --- | --- |
| `rule` | **Hot-injected into `<operational_rules>` on every Phase 2 remediation prompt.** Treated as a hard invariant across all incidents. | Mandatory fleet guardrails (e.g., node-drain exclusions, change freezes, critical-path constraints). |
| `procedure` / `runbook` | Indexed in `<runbook_catalog>` (`id`, `title`, `symptoms`, `summary`) and retrieved on demand by the Phase 2 agent via `qdrant_search` / `qdrant_search_runbooks`. | Step-by-step diagnostics, approved SAG remediation tool names, and escalation thresholds. |
| `postmortem` | Indexed in the catalog and retrieved conditionally when matching past symptoms or service tags appear. | Historical incident context, root causes, and verified lessons learned. |

> [!TIP]
> **Continuous Learning from Reviewed Postmortems:** Aegis deliberately keeps generated `report.md` files out of RAG until a human reviews them (so unvetted outputs never self-reinforce). Whenever your team finishes reviewing a closed incident's postmortem, copy it into `seed/runbooks/` with YAML frontmatter (`types: [postmortem]`, matching `symptoms`, and service `tags`) and sync Qdrant (`docker exec aegis-api aegis seed runbooks --strict` or approve the detected runbook drift). From that moment onward, Phase 2 agents retrieve those real lessons automatically—speeding up root-cause matching and avoiding previously unsuccessful fixes.

### 4.2 Prompt Economics & the Rule Linter Guardrail

Because every `type: rule` (or `tags: [rule]`) document is injected verbatim into every Phase 2 prompt regardless of service, bloated rules degrade reasoning quality and inflate token cost across every incident.

Default linter bounds (`runbook_linter` in `config/sag_policies.yaml`):
- **Max rules:** `8`
- **Max words:** `150`
- **Max characters:** `800`

Keep `rule` documents short and atomic, and move command examples or multi-step diagnostics into `procedure`/`runbook` documents. Run `docker exec aegis-api aegis seed runbooks --strict` to enforce these bounds non-interactively.

### 4.3 Isolating Unrelated Applications with Dedicated Qdrant Collections

**Recommendation:** Whenever Aegis monitors or seeds knowledge for different or unrelated applications, clusters, or microservices architectures, **create and target separate Qdrant collections** (`--collection <name>`) rather than mixing all runbooks into one shared collection (`aegis_runbooks`).

- **Hybrid Runbook Catalog Modes (`qdrant.catalog_mode`):** `<operational_rules>` (`type: rule`) are always direct-injected into every Phase 2 system prompt. In default `auto` mode, non-rule catalog metadata is inlined into `<runbook_catalog>` when `len(index) <= qdrant.catalog_inline_max_entries` (default `15`), and automatically switches to 2-step retrieval (`search_runbook_catalog` → `qdrant_search`) for larger runbook collections (`auto` | `direct` | `two_step`).
- **SHA-256 Digest Versioning & Drift Governance:** Both Topology snapshots (`topology:meta`) and compiled Runbook indexes (`runbooks:index:meta` + companion `seed/.runbooks_index_meta.json`, keeping `seed/runbooks_index.json` as a clean JSON list) track canonical SHA-256 digests and monotonic versions. Drift checks (`GET /v1/drift/status`, `POST /v1/drift/check`, or startup drift scan) stage pending drift (`drift:pending:*`) and notify the operator instead of silently overwriting; operators approve or reject via Telegram (`/approve_drift`, `/reject_drift`) or Control Plane API / Dashboard (`POST /v1/drift/{target}/approve`, `/reject`), or trigger `Manual Sync` / `Force Rebuild` (`POST /v1/drift/{target}/sync?force=false|true`, `POST /v1/topology/refresh?force=true`). Phase 2 hot-reloads `runbook_index` automatically whenever `runbooks:index:meta` changes.
- **How:**
  ```bash
  # Seed distinct collections per application/service domain
  docker exec aegis-api aegis seed runbooks --runbooks-dir /app/seed/runbooks_app_a --collection aegis_runbooks_app_a --strict
  docker exec aegis-api aegis seed runbooks --runbooks-dir /app/seed/runbooks_app_b --collection aegis_runbooks_app_b --strict
  ```
  Then set `qdrant.collection` in `config/system.yaml` to the collection active for that Aegis deployment (or use `--force` when switching embedding models/dimensions).

### 4.4 To Customize Runbooks, RAG Embeddings & Vector Databases

- **Add/Edit Runbooks:** Place Markdown files with YAML frontmatter in `seed/runbooks/` and run `docker exec aegis-api aegis seed runbooks --strict` (add `--force` if changing embedding dimensions). Incremental delta hashing (`seed/.seed_cache.json`) skips unchanged documents on routine runs.
- **Swap Embedding Provider or Model:** Edit `qdrant.embedding_provider` (`ollama`, `openai`, `gemini`), `qdrant.embedding_model`, and `embeddings.*` in `config/system.yaml` (implemented in `shared/harness/embeddings.py` -> `build_embeddings()`), then re-seed with `--force` to recreate the Qdrant collection with the updated vector dimensionality.
- **Point to an External Qdrant Cluster or Custom Vector Store:** Update `qdrant.url` in `config/system.yaml` (or `QDRANT_URL` in `deploy/.env`) to connect to managed Qdrant Cloud or an external cluster. To swap Qdrant for another vector store, implement a matching `qdrant_search` tool endpoint in `services/mcp/tools/` and update `scripts/seed_runbooks.py` / `shared/harness/embeddings.py`.

### 4.5 To Customize the Postmortem Report Format

Phase 3 Scribe report formatting (`services/phase3_worker/graph.py`) combines customizable templates with deterministic ledger-grounded safeguards:

1. **Markdown Layout, Headings & Scorecard Template (`seed/prompts/incident_report_template.md`):**
   Edit section headings, metadata bullets, placeholder fields (`{incident_id}`, `{severity}`, `{affected_services}`, `{root_cause_hypothesis}`, `{timeline_table}`, `{action_plan_with_outcomes}`, `{signals_summary}`, etc.), and scorecard prompts directly in `seed/prompts/incident_report_template.md`.
2. **Scribe Agent Persona & Narrative Style (`seed/prompts/system_scribe_agent.md`):**
   Customize tone, brevity, and domain-specific summary instructions in `seed/prompts/system_scribe_agent.md`. Verify your prompt edits with `docker exec aegis-api aegis seed prompts`.
3. **Deterministic Section Builders (`services/phase3_worker/report_builder.py` & `validator.py`):**
   Core compliance sections (`build_status_badge`, `build_timeline_table`, `build_hypothesis_evolution`, `build_actions_section`, `enforce_sections`, and the unfilled 6-KPI scorecard) are enforced programmatically in Python from ledger entries so models cannot omit actions or fabricate timestamps. To customize timeline columns, KPI scorecard rows, or action formatting, update those builders in `services/phase3_worker/report_builder.py` (and matching checks in `validator.py`).

---

## 5. Safety Action Gateway (SAG), Tooling & Default-Deny Access Boundaries

### 5.1 Default-Deny Philosophy (Zero-Trust Tool Scoping & Allowlists)

Aegis applies a **strict Default-Deny (allowlist-only) model** across tool discovery, agent binding, and execution—nothing is permitted unless explicitly allowed in YAML policy:

1. **MCP Server Registry Allowlist (`config/mcp.yaml`):**
   Agents can only bind tools explicitly listed under a registered `mcp_servers` entry in `config/mcp.yaml`. Unregistered MCP servers or unlisted tool names are ignored.
2. **Agent Tag-Based Scoping (`config/agents.yaml` `tool_selector`):**
   Every agent uses `tool_selector` (`match_any` + `exclude`) to filter tools by tag (`services/phase1_worker/agents/base.py`):
   - A tool is **denied by default** unless at least one of its tags matches `match_any`.
   - Any tool carrying a tag in `exclude` (e.g., `exclude: [write]`) is unconditionally stripped.
   - Read-only Phase 1 investigators additionally drop any tool marked `requires_approval: true` or `pre_approved: false` in `config/sag_policies.yaml`, guaranteeing Phase 1 can never access mutating tools even if mis-tagged.
3. **Gateway Execution Allowlist (`config/sag_policies.yaml`):**
   - `default_requires_approval: true` ensures any unknown or unconfigured tool defaults to blocking execution without human sign-off.
   - Mutations are restricted to explicit `allowed_namespaces`, tool-specific bounds (`max_replicas`, `allowed_environments`, increase ratios), and valid approved `action_id` bindings.

### 5.2 Read Tools vs. Approval-Gated Write Tools

- **Pre-Approved Read Tools (`requires_approval: false`):** PromQL queries (`prometheus_query`, `prometheus_query_range`), LogQL (`loki_query`, `loki_query_range`), traces (`tempo_search`, `tempo_get_trace`, `jaeger_search_traces`, `jaeger_get_trace`), Kubernetes diagnostics (`k8s_get_events`, `k8s_get_pod_status`, `k8s_get_pod_diagnostics`, `k8s_get_node_metrics`, `k8s_get_configmap_diff`, `get_pod_logs`, `get_service_endpoints`), Git/ArgoCD inspection, topology queries (budgeted by `topology.query_budget_per_incident: 5`), and Qdrant search.
- **Write Tools (`requires_approval: true` by default):**
  - `restart_pod`
  - `scale_deployment`
  - `rollback_release` (alias: `rollback_deployment`)
  - `update_resource_limits` (aliases: `patch_deployment_resources`, `patch_resource_limits`)
  - `drain_node` (`strict_confirmation: true`, `severity_override: true` — always requires human approval regardless of severity)
  - `toggle_feature_flag` (alias: `set_feature_flag`)

### 5.3 Two-Layer Defense-in-Depth Validation

All LLM-generated parameters are treated as untrusted input and validated both in-process (`shared/harness/sag_guard.py`) and at the MCP server layer (`shared/sag/validator.py`):

1. **System Halt & Approval Binding:** `incident_id` and `action_id` are server-injected, verified against Redis `"approved"` status (and matched against the approved tool list), and immediately rejected under System Halt or any prior denial (**Deny Wins**).
2. **Injection & Format Rejection:** Identifiers are validated against Kubernetes DNS-1123 rules and scanned for shell/prompt-injection characters (`;`, `&&`, `||`, `` ` ``, `$(`, `<`, `>`, `ignore`, `forget`, `system:`, `__import__`).
3. **Two-Tier Namespace & Incident Scope Enforcement:** Targets must belong to `allowed_namespaces` in `config/sag_policies.yaml` *and* match the incident's affected namespace/service.
4. **Quantitative & Cooldown Guardrails:**
   - `scale_deployment`: Bounded to `1` through `max_replicas` (default `20`).
   - `update_resource_limits`: Normalizes memory quantities (e.g., `512MB` → `512Mi`) and caps changes via `max_cpu_increase_ratio` / `max_memory_increase_ratio` (default `2.0x`).
   - `drain_node`: Enforces `60–600s` grace period and `strict_confirmation: true`.
   - `toggle_feature_flag`: Restricts flag names to `[a-zA-Z0-9_.-]` and environments to `allowed_environments` (`production`, `staging`, `development`).
   - Per-tool `cooldowns` in `config/sag_policies.yaml` throttle repeated mutations.
5. **Approval Timeouts (`approval_matrix`):** `P1` and `P2` default to `900s` timeout and auto-escalate (`default_on_timeout: escalate`); `P3` and `P4` have configurable `auto_approve_pre_approved` rules for pre-approved policies.

### 5.4 Multi-Entity RBAC & Identity Registry (`config/identities.yaml`)

Copy `config/identities.yaml.example` to `config/identities.yaml` to move beyond a single shared `AEGIS_API_KEY` and enforce scoped access across multiple identities (`shared/auth/registry.py`):

- **Supported Entity Types:**
  - `operators`: Human engineers with `role` set to `admin`, `operator`, or `viewer`, individual API keys (`api_key_env`), and optional Telegram bindings (`telegram_user_id_env`).
  - `webhooks`: Alert ingestion sources (e.g., Alertmanager) scoped strictly to `alerts:ingest`.
  - `external_clients`: Third-party integrations restricted to explicit read scopes (e.g., `incidents:read`, `system:read`).
  - `internal_services`: Trusted container-to-container callers (e.g., `phase1-worker`, `phase2-worker`, `phase3-worker`, `aegis-telegram-bot`, `aegis-mcp`, `aegis-topology-mcp`, `aegis-api`, `aegis-cli`).
- **Default Role Scopes (`ROLE_DEFAULT_SCOPES`):**
  - `admin`: `["*"]` (full control, including configuration mutations).
  - `operator`: `incidents:read`, `incidents:write`, `approvals:decide`, `system:read`, `system:halt`, `topology:read`, `config:read`, `audit:read`.
  - `viewer`: `incidents:read`, `system:read`, `topology:read`, `config:read`, `audit:read`.
- **Secrets & Activation Invariant:** Never store raw API keys or personal Telegram user IDs inside `config/identities.yaml`. Reference environment variable names via `api_key_env` and `telegram_user_id_env` and store actual values in `deploy/.env`. An identity entry becomes active only when its referenced environment variable is set (if `config/identities.yaml` is absent, Aegis falls back to legacy `AEGIS_API_KEY` authentication).

### 5.5 To Customize MCP Tools, Transports, Auth & Observability Backends

Aegis connects to both bundled MCP servers and third-party/external MCP tools via `config/mcp.yaml` (`shared/config/schemas.py` & `shared/harness/mcp.py`):

- **Transports Supported:**
  - `streamable_http` *(default)* or `http`: Standard Streamable HTTP MCP endpoint (`server_url`).
  - `sse`: Server-Sent Events endpoint (`server_url`).
  - `stdio`: Local subprocess MCP server spawned inside the worker (`command`, `args`, optional `env` and `cwd`).
- **Authentication & Custom Headers:**
  Supports `${ENV_VAR}` and `${ENV_VAR:-default}` interpolation inside `config/mcp.yaml` (plus `api_key_env`), with multiple auth modes:
  - `auth_type: bearer` *(default)* → `Authorization: Bearer <api_key>`
  - `auth_type: api_key` / `header` → `X-API-Key: <api_key>` (or custom `auth_header`)
  - `auth_type: basic` → `Authorization: Basic <base64>`
  - `auth_type: none` + `headers: { ... }` for arbitrary custom headers.
  - Set `external: true` on external HTTP/SSE servers so `AEGIS_MCP_URL` in Docker Compose does not rewrite their URL to the internal `aegis-mcp` gateway.
- **Important Caveats When Adding or Swapping MCP Tools:**
  1. Register every tool under `mcp_servers.<server>.tools` with accurate `tags` (`[metrics, read]`, `[logs, read]`, `[k8s, write, remediation]`, etc.) so `tool_selector` in `config/agents.yaml` exposes it to the right agents.
  2. Add corresponding policy entries under `tools:` in `config/sag_policies.yaml` (explicitly setting `requires_approval: false`, `pre_approved: true` for read tools, or bounds/namespaces for write tools) and keep `write` tags excluded from Phase 1 agents.
  3. Ensure custom tool argument names and JSON schemas align with `tool_parameters` hints in `seed/prompts/` (e.g., if replacing Prometheus/Loki/Kubernetes tools with custom backends, check that Phase 2 prompts and `convergence.py` know the expected tool/parameter names).

> [!TIP]
> **Adapting Aegis Beyond the Default Stack (Any Domain via MCP + Prompts + RAG):** Because Aegis separates orchestration from tooling and domain knowledge, you can repurpose Aegis for different architectures or non-Kubernetes workflows without rewriting core engine logic:
> 1. **Plug In Your Own Data & Action Sources (`config/mcp.yaml` + `config/sag_policies.yaml` + `config/agents.yaml`):** Register any external MCP servers (`streamable_http`, `sse`, or `stdio`)—such as Datadog, Splunk, PagerDuty, AWS/cloud APIs, Terraform, database admin tools, or internal runbooks—and map their tool tags via `tool_selector` and SAG boundaries.
> 2. **Tune Agent Reasoning & Outputs (`seed/prompts/`):** Small, targeted edits to the Markdown agent prompts (`system_remediation_agent.md`, Phase 1 investigator prompts, `system_scribe_agent.md`, and `incident_report_template.md`) change how agents investigate, reason, and format postmortem reports.
> 3. **Customize Institutional Knowledge (`seed/runbooks/` + Qdrant):** Seed domain-specific `rule`, `runbook`, and reviewed `postmortem` documents into a dedicated Qdrant collection so agents operate accurately on your chosen stack.

### 5.6 To Customize Agent LLMs & Add New Model Providers

- **Mix and Match Providers per Agent (`config/agents.yaml`):**
  Every agent role (`infrastructure_investigator`, `log_trace_investigator`, `change_config_investigator`, `remediation`, `scribe`) independently declares a `primary` model (`provider`, `model`, `temperature`, `token_limit`, optional `base_url`) and an ordered `fallback` chain plus `retry_on` / `token_budget` / `max_tool_calls`. Out of the box, Aegis supports **`gemini`**, **`openai`**, and **`ollama`**.
- **Plug In an Additional LLM Provider (`shared/harness/llm.py`):**
  Because Aegis constructs all models through LangChain's `BaseChatModel` interface in `_create_single_model()` (`shared/harness/llm.py`):
  1. Add the LangChain integration package (e.g., `langchain-anthropic` or `langchain-aws`) to `pyproject.toml`.
  2. Add an `elif provider == "<your_provider>":` branch in `_create_single_model()` reading credentials from `provider_cfg.api_key` or environment variables and returning the LangChain `BaseChatModel` instance.
  3. Reference the new `provider` and `model` name in `config/agents.yaml`. Fallback chaining (`.with_fallbacks()`) and token callbacks work automatically for any valid `BaseChatModel`.

---

## 6. Multi-Channel Operations, Security Hygiene & Day-2 CLI

### 6.1 Telegram Bot Operations

`aegis-telegram-bot` authenticates exclusively against the configured `TELEGRAM_CHAT_ID` (empty allowlist denies all updates) and communicates with the Control Plane over internal port `8081` (`X-Aegis-Service: aegis-telegram-bot`). Operator attribution resolves cleanly in priority order: mapped Aegis operator `username` via `telegram_user_id_env` (`config/identities.yaml` → `deploy/.env`) → Telegram `@username` → Telegram `full_name` → `telegram-<id>`.

- **Automated Pushes:** Incident creation, top-3 Phase 1 triage findings, approval requests (`[✅ Approve]`, `[❌ Deny]`, `[🔄 Steer Plan]` with structured runbook references), cross-channel decision updates, escalations, final postmortem reports (`report.md`), and `drift_detected` alerts for topology or runbook SHA-256 drift.
- **Slash Commands:**
  - `/start` or `/status` — Control plane health and open incident summary
  - `/incidents` (or `/list`) — List open and recent incidents
  - `/incident <id>` — Detailed incident state and findings summary
  - `/approve <id> [reason]` — Authorize pending action
  - `/deny <id> [reason]` — Deny pending action and escalate
  - `/steer <id> <instruction>` — Inject steering guidance and re-plan
  - `/escalate <id> <reason>` — Force-escalate an incident
  - `/close <id> [reason]` — Manually close an incident
  - `/drop <id> [reason]` — Drop incident and cancel worker processing
  - `/approve_drift <topology|runbooks>` — Approve pending SHA-256 drift update and apply sync
  - `/reject_drift <topology|runbooks> [reason]` — Dismiss pending drift without overwriting active snapshots
  - `/help` — Command reference

### 6.2 Operational Dashboard (`http://localhost:3000`)

Log in with your personal Operator API key (or default admin `AEGIS_API_KEY`; verified via `GET /v1/auth/me` and stored in `sessionStorage`). The Control Plane binds every mutation's `actor` / `decided_by` authoritatively to the authenticated username and records `key_id` + inbound/outbound interactions in Redis (`stream:audit:interactions`, inspectable at `GET /v1/audit/interactions`). Key views include:
- **Incidents View:** Filter by state, severity, service, search query, or datetime window; sort columns; trigger quick `Deny`/`Drop` or `+ Demo Incident`.
- **Incident Detail View:** State progression stepper, Approval Queue (with Approve/Deny/Steer modals, matched-height Remediation Proposal & Tool Parameter Payload cards, re-approval for denied proposals, and late-denial support; non-actionable controls such as `Deny` are automatically hidden in terminal/non-actionable states like `CLOSED` or `ESCALATED`), Findings & Ledger timeline with JSON inspection, `report.md` viewer, and Lifecycle Controls (`Standby / Pause`, `Resume`, `Steer`, `Force Escalate`, `Drop Issue`, `Manual Close`).
- **Topology Tab:** Interactive service dependency graph highlighting active incident blast radius.
- **System Halt Toggle:** Header switch triggering fleet-wide emergency standby and displaying a persistent alert banner.
- **Config Tab:** Inspect and edit LLM routing, provider keys (masked on read), SAG policies, MCP servers, and system settings with an **"Old → New" diff confirmation modal** and 1-click version rollback. Under **System**, view the **Identities & Scoped Access** summary (`Key Env`, `TG Env: ... (LINKED / UNSET)`, webhooks, external clients, internal port `8081` services) and the **Topology & Runbook Digest Versioning (SHA-256)** panel (`Check Drift`, `Approve Drift`, `Reject`, `Manual Sync`, `Force Rebuild` with inline feedback).

### 6.3 Config Versioning & Local Storage Security Risk

> [!CAUTION]
> **Security & Git Hygiene Risk (`config/history/` & `data/`):**
> Whenever configurations are updated via the Dashboard **Config Tab** (`services/api_server/routers/config_routes.py`), Aegis writes timestamped full-file YAML snapshots plus `manifest.json` onto local disk inside **`config/history/`** (retaining the latest **8** snapshots per config file and automatically deleting evicted YAML snapshot files from disk). Separately, closed incident ledgers, reports, and topology snapshots are persisted inside **`data/`** (`data/incidents/`, `data/topology_snapshot.json`).
> - **Risk:** Any API key, bot token, or cluster credential entered directly into `config/*.yaml` instead of `deploy/.env` is copied verbatim into plaintext snapshot files inside `config/history/` on subsequent edits and survives rollback/cleanup unless purged manually. Keep secrets and personal IDs exclusively in `deploy/.env` (referenced via `api_key_env` / `telegram_user_id_env` in `config/identities.yaml` or `${VAR}` in `config/mcp.yaml`).
> - **Mitigation:**
>   1. Store all secrets and personal IDs (`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `AEGIS_API_KEY`, `AEGIS_ADMIN_TELEGRAM_ID`, `KUBERNETES_TOKEN`, `ARGOCD_TOKEN`, `GITHUB_TOKEN`, external MCP `api_key` values) strictly in **`deploy/.env`** or environment variables (using `${VAR}` interpolation in `config/mcp.yaml`).
>   2. Never commit `config/history/`, `config/identities.yaml`, `seed/.runbooks_index_meta.json`, `data/`, `.env`, `deploy/.env`, or cluster kubeconfig/token files to version control (verify they remain listed in `.gitignore`, while keeping `.example` templates tracked).
>   3. Restrict host filesystem permissions on `config/history/` and `data/` in shared or production environments, and scrub old snapshots if a credential is ever rotated.

### 6.4 Day-2 CLI Operations & Operational APIs

Run all Day-2 operational commands through the single standardized entrypoint inside the running `aegis-api` container, and use Control Plane GET endpoints (`GET /v1/audit/interactions`, `GET /v1/alerts/dropped`, `GET /v1/drift/status`) for live audit/drop/digest inspection:

| Task | Command | Purpose |
| --- | --- | --- |
| **Verify Stack Health** | `docker exec aegis-api aegis verify` | Checks connectivity and latency for Redis, Qdrant, Ollama, and all MCP servers (`HTTP`/`SSE` health checks and `STDIO` command resolution) configured in `config/mcp.yaml`. |
| **Initialize Topology** | `docker exec aegis-api aegis init --helm-dir <path> --namespace <ns>` | Parses Helm manifests into dependency nodes/edges and saves to Redis + `/data/topology_snapshot.json`. |
| **Seed Runbooks** | `docker exec aegis-api aegis seed runbooks --strict` | Lints `type: rule` limits, compiles `seed/runbooks_index.json`, and upserts embeddings into Qdrant (supports `--collection <name>` and `--force`). |
| **Validate Prompts** | `docker exec aegis-api aegis seed prompts` | Verifies all required agent personas, tasks, and Markdown report/reasoning templates are present and valid. |
| **Reload Alert Rules** | `docker exec aegis-api aegis seed alert-rules` | Validates Prometheus alerting rules YAML and triggers `POST /-/reload` on the configured Prometheus URL. |
| **Inspect MCP Registry** | `docker exec aegis-api aegis seed mcp-config` | Displays all registered MCP servers, transport URLs/commands, and configured tools. |
