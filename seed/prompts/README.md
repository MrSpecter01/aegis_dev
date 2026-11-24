# Seed Prompts Directory

This directory is the single canonical source of truth for agent personas and task templates:

- `agents.yaml` — Defines CrewAI agent personas (`role`, `goal`, `backstory`, `max_iter`, `max_tool_calls`, `token_budget`, `allow_delegation`, `verbose`) for all specialist agents.
- `tasks.yaml` — Defines structured task descriptions, contextual parameter placeholders (`{service}`, `{namespace}`, `{alert_context}`, `{topology_subgraph}`, `{severity}`), and expected outputs.

All prompts are loaded via `shared.prompts.loader` (`load_agent_prompt`, `load_task_prompt`), supporting path overrides via the `AEGIS_PROMPTS_DIR` environment variable.
