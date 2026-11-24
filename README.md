# Aegis

**Autonomous AI-Powered Site Reliability Engineering (SRE) & Incident Response Platform**

Aegis is a modular, multi-service incident response platform that automates alert triage, parallel root-cause investigation, policy-guarded remediation, and post-incident reporting—while keeping human operators firmly in control through deterministic safety guardrails and an append-only audit ledger.

---

## Table of Contents

- [Overview & Philosophy](#overview--philosophy)
- [Interaction Layers & Architecture](#interaction-layers--architecture)
- [Modular & Plug-and-Play by Design](#modular--plug-and-play-by-design)
- [Built With](#built-with)
- [Prerequisites & Resource Requirements](#prerequisites--resource-requirements)
- [Installation](#installation)
- [Usage Guide](#usage-guide)
  - [1. Try Out Aegis (Pre-Seeded Quick Start)](#1-try-out-aegis-pre-seeded-quick-start)
  - [2. Configure Aegis for Your Own System](#2-configure-aegis-for-your-own-system)
- [Configuration Files](#configuration-files)
- [Project Structure](#project-structure)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## Overview & Philosophy

During an outage, operators face a flood of correlated alerts, logs, metrics, and traces across distributed services. Manually triaging downstream symptoms, looking up runbooks, and verifying safe recovery actions under pressure increases Mean Time to Resolution (MTTR).

**Aegis** is built around a simple philosophy: **let autonomous AI agents handle high-speed investigation and correlation, while deterministic guardrails and human operators govern every state-changing action.**

Every incident follows a structured three-phase lifecycle backed by an immutable Redis ledger:

1. **Phase 1 — Triage & Topological Correlation (`phase1-worker`):**
   Ingests firing alerts, deduplicates and groups related signals within a configurable window, maps affected services against the target deployment's dependency graph to isolate upstream root-cause candidates, and produces initial triage context.
2. **Phase 2 — Multi-Agent Investigation & Guarded Remediation (`phase2-worker`):**
   Dispatches specialized domain investigators in parallel across infrastructure metrics, logs/traces, and configuration/deployment history. Their findings converge into a root-cause hypothesis and a proposed remediation action. Before any action can run, it must pass the deterministic **Safety Action Gateway (SAG)** and—when policy requires—receive explicit **Human-in-the-Loop (HITL)** operator approval, followed by automated post-action bake-time verification.
3. **Phase 3 — Post-Incident Scribe & Knowledge Indexing (`phase3-worker`):**
   Synthesizes a structured Markdown post-mortem report from the append-only incident ledger after resolution or escalation, and indexes verified learnings back into the vector store.

---

## Interaction Layers & Architecture

Aegis separates orchestration from the target environment and operator channels through clear interaction boundaries:

![Aegis System Architecture Overview](docs/diagrams/system-overview.svg)

*High-level overview of Aegis entry points, internal trust boundaries, autonomous pipeline, and external integrations ([Interactive Overview](docs/diagrams/system-overview.html) · [All Architecture Diagrams](docs/diagrams/README.md)).*

- **Control Plane & Alert Ingestion:** Receives alerts from Prometheus Alertmanager or external alerting pipelines, correlates incidents, manages the incident lifecycle, and provides the operator CLI.
- **Target System & Observability Gateway (MCP & Safety):** Mediates all agent interactions with the observed environment through policy-governed read tools (metrics, logs, distributed traces, Git/GitOps history, service topology) and approval-gated write tools for target remediation.
- **State, Event Bus & Knowledge Layer:** Combines operational state, async worker streams, and an append-only audit ledger with a vector knowledge store for operational runbooks, domain rules, and historical incidents.
- **Operator Interaction Layer (HITL):**
  - **Operations Dashboard:** Real-time dependency topology view, incident timeline, agent findings, and interactive **Approve**, **Deny**, **Steer** (inject custom investigation guidance), **Pause/Resume**, **Drop**, and global **System Halt** controls.
  - **Telegram Bot *(Optional)*:** Bidirectional chat notifications and inline approval/steering commands for on-call engineers.
  - **Observability UI *(Optional Bundled Stack)*:** Pre-provisioned dashboards for viewing target telemetry alongside Aegis operational metrics.

> For service ports, API endpoints, and deployment networking details, see the **[Operator Manual](docs/operator_manual.md)**.

---

## Modular & Plug-and-Play by Design

Aegis is designed so that you can connect it to almost any target system or AI/observability stack primarily through configuration updates rather than core code rewrites. Key examples include:

- **Per-Agent LLM Routing (`config/agents.yaml`):** Mix and match cloud or local models (Google Gemini, OpenAI, or local Ollama) per agent role with automatic fallback chains and temperature controls—or extend the LangChain model factory in `shared/harness/llm.py` to plug in additional providers.
- **Swappable MCP Tooling & Observability (`config/mcp.yaml`, `config/sag_policies.yaml`):** Use Aegis's bundled Prometheus/Loki/Tempo stack or point the MCP servers at your existing external observability and deployment endpoints. Read/write tool access is tag-scoped per agent and governed declaratively by SAG policies.

>Runbooks, RAG embedding models/stores, and additional operator notification/approval channels (via LangGraph workflow nodes) are equally modular. See the **[Operator Manual](docs/operator_manual.md)** for full customization guides across each subsystem.

---

## Built With

- **Backend & Agent Orchestration:** Python 3.12, FastAPI, LangGraph, CrewAI, LangChain, Pydantic v2, Typer
- **Tooling Interface:** Model Context Protocol (MCP)
- **State & Vector Databases:** Redis 7, Qdrant
- **Bundled Telemetry Stack (Optional Profile):** Prometheus, Alertmanager, Grafana, Loki, Tempo
- **Operator Interfaces:** React + TypeScript + Vite (Web UI), `python-telegram-bot` (Telegram HITL)
- **Supported LLM Backends:** Google Gemini, OpenAI, Ollama

---

## Prerequisites & Resource Requirements

Aegis runs entirely inside Docker containers—**you do not need Python, `pip`/`uv`, or a local Kubernetes (`kind`) cluster installed on your host machine to build and run Aegis.** The target system being observed can live anywhere (remote cloud cluster, on-premises, or a local test cluster) as long as Aegis can reach its observability and management endpoints.

### 1. Required Software
- **Docker** & **Docker Compose** (v2+)
- **Git**

### 2. Runtime Resource Requirements (Getting Started)

| Deployment Mode | CPU | Free Memory (RAM) | Disk Storage |
| --- | --- | --- | --- |
| **Core Stack** *(Aegis services + Redis, Qdrant, Grafana)* | 2+ vCPUs | ~2 – 4 GB | ~3 GB |
| **Core + Bundled Monitoring** *(`--profile monitoring`)* | 4+ vCPUs | ~4 – 6 GB | ~5 GB |
| **Full Local w/ Containerized Ollama** *(`--profile ollama`)* | 4–8+ vCPUs (or GPU) | ~8 – 12+ GB | ~10+ GB (includes model weights) |

> **Hardware Acceleration Note:** Host-installed **Ollama** auto-detects and uses your GPU by default, whereas the containerized `--profile ollama` service in `deploy/docker-compose.yml` defaults to **CPU** unless NVIDIA/GPU passthrough (`deploy.resources.reservations.devices`) is added. **Qdrant** runs on CPU.

### 3. API Keys & Access Tokens
- **LLM API Keys:** An active API key for your configured model provider (e.g., `GOOGLE_API_KEY` or OpenAI API key) or a reachable Ollama server.
  - *Token Budget Note:* A single end-to-end incident investigation and remediation run typically consumes **~27K–32K tokens** across all participating agents.
- **Target & Notification Credentials *(for live operations)*:**
  - Read/write access credentials for your target infrastructure and observability stack (e.g., `KUBECONFIG_PATH` / `KUBERNETES_TOKEN`, `ARGOCD_TOKEN`, `GITHUB_TOKEN`).
  - Telegram Bot token (`TELEGRAM_BOT_TOKEN`) and authorized chat ID (`TELEGRAM_CHAT_ID`) if using Telegram HITL controls.

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/MrSpecter01/aegis_dev.git
cd aegis_dev
```

### 2. Configure Environment & Identity Templates

Copy the environment and RBAC templates to your local untracked files, then add your LLM API key and any desired integration tokens to `deploy/.env`:

```bash
cp deploy/.env.example deploy/.env
cp config/identities.yaml.example config/identities.yaml
```

### 3. Build and Start with Docker Compose

Build the shared base image first, then spin up the Aegis stack:

```bash
docker compose -f "deploy/docker-compose.yml" build aegis-base
docker compose -f "deploy/docker-compose.yml" up -d --build
```
After the first build, you can omit `--build` unless Dockerfiles or shared dependencies change. First-time image builds and service startup may take a few minutes depending on hardware/network—wait until `docker ps` shows `aegis-api`, `aegis-redis`, `aegis-qdrant`, and `aegis-mcp` as `(healthy)` before proceeding.

*(Note: By default, `deploy/.env.example` sets `COMPOSE_PROFILES=monitoring` so the bundled Prometheus, Alertmanager, Loki, and Tempo containers start automatically. Edit `COMPOSE_PROFILES` in `deploy/.env` or pass `--profile` flags to customize which optional tiers start.)*

---

## Usage Guide

### 1. Try Out Aegis (Pre-Seeded Quick Start)

The repository ships with **pre-seeded sample assets**—including sample SRE runbooks (`seed/runbooks/`), agent prompts (`seed/prompts/`), Prometheus alert rules (`seed/alert_rules/`), and a sample microservice Helm chart (`charts/online-boutique`)—so you can test the full incident workflow immediately. *(LLM API keys are not included; supply your own key in `deploy/.env`.)*

1. **Initialize the sample topology, seed runbooks into Qdrant, and verify health** (executed directly inside the `aegis-api` container):
   ```bash
   docker exec aegis-api aegis init --helm-dir /app/charts/online-boutique --namespace boutique
   # Expected: topology nodes/edges parsed and saved with SHA-256 digest

   docker exec aegis-api aegis seed runbooks --strict
   # Expected: strict rule lint passes + Qdrant chunks seeded (first run embeds via Ollama/provider; may take ~15-60s)

   docker exec aegis-api aegis seed prompts
   # Expected: prompt templates validated with zero missing files

   docker exec aegis-api aegis verify
   # Expected: Redis, Qdrant, Ollama/LLM, and MCP servers report reachable/OK
   ```

2. **Open the Operator Interfaces:**
   - **Web Operator Dashboard:** `http://localhost:3000` (log in with `AEGIS_API_KEY` from `deploy/.env`)
   - **Control Plane REST API Docs:** `http://localhost:8080/docs`
   - **Grafana:** `http://localhost:3001` (default credentials: `admin` / `admin`)

3. **Simulate an Incoming Alert:**
   Send a sample Alertmanager payload to trigger an incident (namespace must match `topology.monitored_namespaces` / `ingestion.allowed_namespaces`, default `boutique`):
   ```bash
   curl -X POST http://localhost:8080/v1/alerts/ingest \
     -H "Content-Type: application/json" \
     -H "X-API-Key: aegis-secret-key-dev" \
     -d '{
       "status": "firing",
       "alerts": [
         {
           "status": "firing",
           "labels": {
             "alertname": "PodHighCpuUsage",
             "service": "frontend",
             "namespace": "boutique",
             "severity": "critical"
           },
           "annotations": {
             "summary": "High CPU usage detected on frontend pod",
             "description": "CPU usage has exceeded 90% for 2 minutes."
           },
           "startsAt": "2026-01-01T00:00:00Z"
         }
       ]
     }'
   # Expected: {"status":"queued","stream_id":"..."} (if "dropped_out_of_scope", add the namespace to topology.monitored_namespaces)
   ```

4. **Interact with the Incident:**
   Watch Phase 1 triage and Phase 2 multi-agent investigation progress in real time on `http://localhost:3000` (or Telegram), and use **Approve**, **Deny**, or **Steer** when Aegis proposes a remediation.

---

### 2. Configure Aegis for Your Own System

To connect Aegis to your own live services or production environment, configure the following components for your target system:

1. **Observability & Read Tools (`deploy/.env`, `config/mcp.yaml`, `config/system.yaml`):**
   Point Aegis to your target metrics, logs, and traces (either by scraping your environment into Aegis's bundled monitoring stack or connecting `aegis-mcp` to your external observability endpoints and read tools).
2. **Action Write Tools (`deploy/.env`, `config/mcp.yaml`):**
   Configure cluster/platform credentials (`KUBECONFIG_PATH`, `KUBERNETES_URL`, `KUBERNETES_TOKEN`, `ARGOCD_TOKEN`, `GITHUB_TOKEN`, or custom MCP write tools) so Aegis can execute approved remediations on the target system.
3. **Alert Rules (`seed/alert_rules/` or external Alertmanager):**
   Define alerting rules for your services and route firing webhooks to `http://<aegis-host>:8080/v1/alerts/ingest` with your `X-API-Key`.
4. **Safety Action Gateway (SAG) Policies (`config/sag_policies.yaml`):**
   Adapt namespace allowlists, resource/replica bounds, cooldown timers, and HITL approval requirements to match your operational risk boundaries.
5. **Runbooks & Operational Rules (`seed/runbooks/`):**
   Add Markdown runbooks and domain rules describing your architecture's failure modes and approved recovery steps, then run `docker exec aegis-api aegis seed runbooks --strict`.
6. **Target Deployment Topology (`charts/` & `aegis init`):**
   Provide the Helm chart manifests (or topology definitions) of your target deployments and run `docker exec aegis-api aegis init --helm-dir <path> --namespace <ns>` so Phase 1 can map service dependencies.
7. **Agent LLM API Keys & Model Selection (`deploy/.env`, `config/agents.yaml`):**
   Supply API keys for your chosen LLM/embedding providers and tune per-agent model assignments and fallback chains.
8. **Telegram Bot Token & Allowed Chat ID (`deploy/.env`):**
   Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to enable real-time incident notifications and chat-based HITL controls.
9. **Agent Prompts (`seed/prompts/`, *optional*):**
   Customize agent persona definitions (`agents.yaml`, `tasks.yaml`) or system prompt templates if your environment requires specialized domain instructions.
10. **Multi-Entity Identities & RBAC (`config/identities.yaml`, *optional*):**
    Copy `config/identities.yaml.example` to `config/identities.yaml` to configure scoped API keys and roles (`admin`, `operator`, `viewer`) for operators, Alertmanager webhooks (`alerts:ingest`), and external read-only clients via environment variable references in `deploy/.env`.

For detailed schemas, tool adaptation notes, and step-by-step setup instructions, consult the **[Operator Manual & SRE Playbook](docs/operator_manual.md)**.

---

## Configuration Files

| Path | Description |
| --- | --- |
| `deploy/.env` | Environment variables, API keys, service tokens, Compose profiles, and host port overrides |
| `config/identities.yaml` | RBAC roles and scopes for operators, webhooks, and external clients referencing env vars in `deploy/.env` |
| `config/system.yaml` | Redis, Qdrant, embedding settings, correlation windows, and telemetry endpoints |
| `config/agents.yaml` | Per-agent LLM provider/model routing, fallback chains, temperature, and MCP tool tag selectors |
| `config/mcp.yaml` | Registered MCP server URLs, timeouts, and read/write tool metadata tags |
| `config/sag_policies.yaml` | Safety Action Gateway rules, namespace restrictions, parameter bounds, and approval policies |

Template files (`*.yaml.example` and `deploy/.env.example`) are included in the repository.

---

## Project Structure

```text
aegis/
├── charts/          # Sample Helm charts for service topology initialization
├── config/          # Declarative YAML configurations (system, agents, mcp, sag_policies)
├── deploy/          # Docker Compose stack, .env.example, and bundled observability configs
├── docs/            # Operator Manual & public documentation
├── scripts/         # Operational helper and runbook seeding scripts
├── seed/            # Pre-seeded SRE runbooks, agent prompt templates, and alert rules
├── services/        # Microservices (api_server, phase1/2/3_worker, mcp, dashboard, telegram_bot)
├── shared/          # Shared Pydantic schemas, LLM/MCP harnesses, SAG validator, and ledger client
├── Dockerfile.base  # Shared Python base image for Aegis backend services
└── pyproject.toml   # Python package metadata and CLI entry point definition
```

---

## Documentation

- **[Architecture Diagrams](docs/diagrams/README.md):** High-level System Overview, Application & Backend, Data & Integration, and Business Capabilities & Interactions views (static SVGs + interactive HTML).
- **[Operator Manual & SRE Playbook](docs/operator_manual.md):** In-depth guide covering the incident lifecycle, HITL operator commands, runbook authoring and linting standards, SAG policy configuration, MCP/LLM/RAG customization, and Day-2 CLI operations.

---

## Contributing

Contributions, bug reports, and feature requests are welcome!

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/your-feature`).
3. Commit your changes (`git commit -m "Add your feature"`).
4. Push to the branch (`git push origin feature/your-feature`).
5. Open a Pull Request.

---

## License

This project is released under the MIT License.
