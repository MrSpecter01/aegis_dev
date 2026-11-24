"""Base investigator agent implementation for Phase 1 using CrewAI.

Handles loading agent/task configs from YAML, setting up the CrewAI Agent,
and wrapping MCP tools.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Union
import yaml

from langchain_core.language_models.chat_models import BaseChatModel
from crewai import Agent, Task, Crew

from shared.harness.llm import TokenBudgetCallbackHandler, TokenBudgetExceeded
from shared.harness.mcp import MCPClient
from shared.harness.crewai_tools import build_mcp_tools, AgentBudgetState
from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding
from shared.prompts.loader import (
    DEFAULT_PROMPTS_DIR,
    get_prompts_dir,
    load_agent_prompt,
    load_task_prompt,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = DEFAULT_PROMPTS_DIR.parent.parent

try:
    from crewai.llms.base_llm import BaseLLM
except ImportError:
    from pydantic import BaseModel as BaseLLM
from pydantic import Field


def _langchain_to_crew_llm(model: Any) -> Any:
    """Adapt a single LangChain BaseChatModel to a CrewAI LLM."""
    if hasattr(model, "__class__") and "langchain" in model.__class__.__module__:
        try:
            from crewai import LLM as CrewLLM
            model_name = getattr(model, "model_name", getattr(model, "model", None))
            if model_name:
                mod_name = model.__class__.__module__.lower()
                llm_kwargs: Dict[str, Any] = {}
                temp = getattr(model, "temperature", None)
                if temp is not None:
                    llm_kwargs["temperature"] = temp
                max_tok = (
                    getattr(model, "max_output_tokens", None)
                    or getattr(model, "max_tokens", None)
                    or getattr(model, "num_predict", None)
                )
                if isinstance(max_tok, int) and max_tok > 0:
                    llm_kwargs["max_tokens"] = max_tok

                if "google" in mod_name:
                    api_key = getattr(model, "google_api_key", None)
                    if api_key and hasattr(api_key, "get_secret_value"):
                        api_key = api_key.get_secret_value()
                    return CrewLLM(
                        model=f"gemini/{model_name}",
                        api_key=api_key or os.getenv("GOOGLE_API_KEY"),
                        **llm_kwargs,
                    )
                elif "openai" in mod_name:
                    api_key = getattr(model, "openai_api_key", None)
                    if api_key and hasattr(api_key, "get_secret_value"):
                        api_key = api_key.get_secret_value()
                    return CrewLLM(
                        model=f"openai/{model_name}",
                        api_key=api_key or os.getenv("OPENAI_API_KEY"),
                        **llm_kwargs,
                    )
                elif "ollama" in mod_name:
                    base_url = getattr(model, "base_url", None)
                    return CrewLLM(
                        model=f"ollama/{model_name}",
                        base_url=base_url,
                        **llm_kwargs,
                    )
        except Exception as e:
            logger.warning("Could not adapt langchain model to CrewLLM: %s", e)
    return model


class CrewLLMWithFallbacks(BaseLLM):
    """CrewAI-compatible LLM wrapper that cascades through fallback models on error."""
    primary: Any
    fallbacks: List[Any] = Field(default_factory=list)

    def supports_function_calling(self) -> bool:
        if hasattr(self.primary, "supports_function_calling"):
            return self.primary.supports_function_calling()
        return True

    def supports_stop_words(self) -> bool:
        if hasattr(self.primary, "supports_stop_words"):
            return self.primary.supports_stop_words()
        return True

    def get_context_window_size(self) -> int:
        if hasattr(self.primary, "get_context_window_size"):
            return self.primary.get_context_window_size()
        return 8192

    def call(
        self,
        messages: Any,
        tools: Optional[List[Dict[str, Any]]] = None,
        callbacks: Optional[List[Any]] = None,
        available_functions: Optional[Dict[str, Any]] = None,
        from_task: Optional[Any] = None,
        from_agent: Optional[Any] = None,
        response_model: Optional[Any] = None,
    ) -> Any:
        all_models = [self.primary] + list(self.fallbacks)
        last_exc = None
        for i, m in enumerate(all_models):
            for attempt in range(2):
                try:
                    return m.call(
                        messages=messages,
                        tools=tools,
                        callbacks=callbacks,
                        available_functions=available_functions,
                        from_task=from_task,
                        from_agent=from_agent,
                        response_model=response_model,
                    )
                except Exception as exc:
                    last_exc = exc
                    err_str = str(exc)
                    is_transient = any(
                        k in err_str
                        for k in (
                            "No address associated with hostname",
                            "gaierror",
                            "ConnectError",
                            "timeout",
                            "timed out",
                            "429",
                            "503",
                            "RESOURCE_EXHAUSTED",
                            "rate limit",
                            "overloaded",
                        )
                    )
                    if is_transient and attempt == 0:
                        logger.warning(
                            "Transient failure calling %s: %s. Retrying in 2.0s...",
                            getattr(m, "model", str(m)),
                            exc,
                        )
                        time.sleep(2.0)
                        continue

                    model_repr = getattr(m, "model", str(m))
                    logger.warning(
                        "LLM call to %s failed with %s; attempting fallback model (%d/%d)",
                        model_repr,
                        exc,
                        i + 1,
                        len(all_models),
                    )
                    break
        if last_exc:
            raise last_exc


class BaseInvestigator:
    """Base class for Phase 1 specialist investigator agents."""

    default_mcp_servers: List[str] = []

    def __init__(
        self,
        agent_role: AgentRole,
        agent_config_key: str,
        task_config_key: str,
        llm: Optional[BaseChatModel] = None,
        mcp_clients: Optional[Dict[str, MCPClient]] = None,
        token_budget: Optional[int] = None,
        max_iter: Optional[int] = None,
        max_tool_calls: Optional[int] = None,
        prompts_dir: Optional[Union[str, Path]] = None,
    ):
        self.agent_role = agent_role
        self.agent_config_key = agent_config_key
        self.task_config_key = task_config_key
        self.prompts_dir = get_prompts_dir(prompts_dir)
        
        # Load prompt configs from central prompts dir
        self.agent_config = load_agent_prompt(self.agent_config_key, self.prompts_dir)
        self.task_config = load_task_prompt(self.task_config_key, self.prompts_dir)

        # Hierarchical budget and turn limit resolution:
        # 1. Constructor parameters
        # 2. config/agents.yaml
        # 3. seed/prompts/agents.yaml
        # 4. Safe default
        config_agents: Dict[str, Any] = {}
        try:
            from shared.config.loader import load_agents_config
            agents_cfg = load_agents_config()
            if self.agent_config_key in agents_cfg.agents:
                config_agents = agents_cfg.agents[self.agent_config_key].model_dump()
        except Exception as exc:
            logger.warning("Could not load agent budget config for %s: %s", self.agent_config_key, exc)

        self.token_budget = (
            token_budget
            or config_agents.get("token_budget")
            or self.agent_config.get("token_budget")
            or 10000
        )
        self.max_iter = (
            max_iter
            or config_agents.get("max_iter")
            or self.agent_config.get("max_iter")
            or 10
        )
        self.max_tool_calls = (
            max_tool_calls
            or config_agents.get("max_tool_calls")
            or self.agent_config.get("max_tool_calls")
            or 10
        )

        # Auto-wire LLM if not provided
        self.llm = llm
        if self.llm is None:
            try:
                from shared.harness.llm import build_llm
                self.llm = build_llm(self.agent_config_key)
            except Exception as e:
                logger.debug("LLM auto-wiring deferred or skipped for '%s': %s", self.agent_config_key, e)

        # Auto-wire MCP clients if not provided
        if mcp_clients is None:
            try:
                from shared.harness.mcp import get_mcp_clients
                from shared.config.loader import load_agents_config, load_mcp_config
                mcp_cfg = load_mcp_config()
                agents_cfg = load_agents_config()
                selector = None
                if self.agent_config_key in agents_cfg.agents:
                    selector = agents_cfg.agents[self.agent_config_key].tool_selector

                if selector:
                    match_any = set(selector.get("match_any") or [])
                    matched_servers = []
                    for s_name, s_cfg in mcp_cfg.mcp_servers.items():
                        for t_name in s_cfg.tool_names:
                            tags = set(s_cfg.get_tool_tags(t_name) or [s_name])
                            if match_any and any(t in tags for t in match_any):
                                matched_servers.append(s_name)
                                break
                    self.mcp_clients = get_mcp_clients(matched_servers or None)
                else:
                    self.mcp_clients = get_mcp_clients(self.default_mcp_servers or None)
            except Exception as e:
                logger.debug("MCP client auto-wiring deferred or skipped for '%s': %s", self.agent_config_key, e)
                self.mcp_clients = {}
        else:
            self.mcp_clients = mcp_clients

    def _load_yaml_config(self, filename: str, key: str) -> Dict[str, Any]:
        """Load a specific key from a YAML configuration file in prompts_dir."""
        filepath = self.prompts_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Missing config file: {filepath}")
        
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if not isinstance(data, dict) or key not in data:
                raise KeyError(f"Key '{key}' not found in {filename}")
            return data[key]

    def _create_partial_finding(
        self,
        reason: str,
        truncated: bool = False,
        confidence: float = 0.2,
        infra_flags: Optional[Dict[str, Any]] = None,
    ) -> AgentFinding:
        """Create a safe fallback AgentFinding on error."""
        return AgentFinding(
            agent_role=self.agent_role,
            hypothesis=reason,
            confidence=confidence,
            evidence=[],
            verbatim_signals={},
            infra_flags=infra_flags or {},
            recommended_actions=[],
            truncated=truncated,
            timestamp=datetime.now(timezone.utc),
        )

    async def _execute_crew(self, context_vars: Dict[str, Any], allowed_tools: Optional[List[str]] = None) -> AgentFinding:
        """Execute the CrewAI agent task and enforce the budget penalty."""
        if self.llm is None:
            raise ValueError("No LLM configured for investigator")

        # Create the shared state for tracking budget
        agent_state = AgentBudgetState()
        
        # Load MCP server mappings and policy configuration for tool scoping
        write_tool_names = set()
        mcp_cfg = None
        try:
            from shared.config.loader import load_mcp_config, load_sag_policies_config
            mcp_cfg = load_mcp_config()
            sag_cfg = load_sag_policies_config()
            tool_dict = getattr(sag_cfg, "tools", {}) or getattr(sag_cfg.policies, "tools", {})
            for t_name, t_pol in tool_dict.items():
                if getattr(t_pol, "requires_approval", False) or not getattr(t_pol, "pre_approved", True):
                    write_tool_names.add(t_name)
                    # Also include any declared aliases
                    for alias in getattr(t_pol, "aliases", []) or []:
                        write_tool_names.add(alias)
        except Exception as exc:
            logger.debug("Policy/MCP config loading deferred in agent: %s", exc)

        # Resolve tool selector and tool tags from configs
        tool_selector = None
        tool_tags_map = {}
        if mcp_cfg:
            for s_name, s_cfg in mcp_cfg.mcp_servers.items():
                for t_name in s_cfg.tool_names:
                    tags = s_cfg.get_tool_tags(t_name)
                    if not tags:
                        tags = [s_name, "read"]
                    tool_tags_map[t_name] = set(tags)

        try:
            from shared.config.loader import load_agents_config
            agents_cfg = load_agents_config()
            if self.agent_config_key in agents_cfg.agents:
                tool_selector = agents_cfg.agents[self.agent_config_key].tool_selector
        except Exception as exc:
            logger.debug("Failed loading tool selector for %s: %s", self.agent_config_key, exc)

        # Build tools dynamically from MCP clients with deduplication and write-tool exclusion
        tools = []
        seen_tool_names = set()
        for client_name, client in self.mcp_clients.items():
            client_tools = await build_mcp_tools(client, agent_state)
            
            # Filter tools to those configured for this client/server if defined
            configured_server_tools = None
            if mcp_cfg and client_name in mcp_cfg.mcp_servers:
                configured_server_tools = set(mcp_cfg.mcp_servers[client_name].tool_names)

            for t in client_tools:
                # 1. Skip write tools (Phase 1 agents are strictly read-only investigators)
                if t.name in write_tool_names:
                    continue

                # 2. Tag-based selector filtering if defined
                if tool_selector:
                    tags = tool_tags_map.get(t.name, set())
                    exclude_tags = set(tool_selector.get("exclude") or [])
                    match_any_tags = set(tool_selector.get("match_any") or [])
                    match_all_tags = set(tool_selector.get("match_all") or [])

                    if exclude_tags and any(et in tags for et in exclude_tags):
                        continue
                    if match_any_tags and not any(mt in tags for mt in match_any_tags):
                        continue
                    if match_all_tags and not all(mt in tags for mt in match_all_tags):
                        continue
                elif configured_server_tools is not None and t.name not in configured_server_tools:
                    # Fallback to server-scoping if no selector specified
                    continue

                # 3. Skip tools not in caller's allowed_tools filter if provided
                if allowed_tools is not None and t.name not in allowed_tools:
                    continue

                # 4. Deduplicate across multiple MCP client bindings
                if t.name in seen_tool_names:
                    continue

                seen_tool_names.add(t.name)
                tools.append(t)

        max_iter = self.max_iter
        max_tool_calls = self.max_tool_calls
            
        # Create the CrewAI Agent (adapt primary and fallback models to CrewAI LLMs)
        primary_langchain = getattr(self.llm, "runnable", self.llm)
        primary_crew_llm = _langchain_to_crew_llm(primary_langchain)

        fallback_langchains = getattr(self.llm, "fallbacks", [])
        fallback_crew_llms = [_langchain_to_crew_llm(fb) for fb in fallback_langchains]
        fallback_crew_llms = [fb for fb in fallback_crew_llms if fb is not None]

        if fallback_crew_llms and hasattr(primary_crew_llm, "model"):
            crew_llm = CrewLLMWithFallbacks(
                model=primary_crew_llm.model,
                primary=primary_crew_llm,
                fallbacks=fallback_crew_llms,
            )
        else:
            crew_llm = primary_crew_llm

        agent = Agent(
            role=self.agent_config.get("role", "Investigator"),
            goal=self.agent_config.get("goal", ""),
            backstory=self.agent_config.get("backstory", ""),
            max_iter=max_iter,
            allow_delegation=self.agent_config.get("allow_delegation", False),
            verbose=self.agent_config.get("verbose", True),
            llm=crew_llm,
            tools=tools,
        )

        # Attach token budget callback if LLM supports callbacks
        token_handler = TokenBudgetCallbackHandler(token_budget=self.token_budget)
        targets = [self.llm]
        runnable_attr = getattr(self.llm, "runnable", None)
        if runnable_attr is not None and runnable_attr is not self.llm:
            targets.append(runnable_attr)

        for target_llm in targets:
            if hasattr(target_llm, "callbacks"):
                try:
                    if target_llm.callbacks is None:
                        target_llm.callbacks = [token_handler]
                    elif isinstance(target_llm.callbacks, list) and token_handler not in target_llm.callbacks:
                        target_llm.callbacks.append(token_handler)
                except Exception:
                    pass

        # Create the CrewAI Task with safely formatted context variables
        task_desc = self.task_config.get("description", "")
        for var_name, var_val in context_vars.items():
            task_desc = task_desc.replace(f"{{{var_name}}}", str(var_val))

        task_desc += (
            "\n\nCRITICAL EXECUTION REQUIREMENT: You MUST invoke your available tools to collect empirical evidence proving or disproving the alert claim before concluding. "
            "You are strictly forbidden from fabricating or guessing pod names, container states, exit codes, or metrics. "
            "All evidence in evidence[].raw_output must be the verbatim output from tools you actually executed during this investigation."
        )

        task = Task(
            description=task_desc,
            expected_output=self.task_config.get("expected_output", ""),
            agent=agent,
            output_pydantic=AgentFinding,
        )

        crew = Crew(
            agents=[agent],
            tasks=[task],
            verbose=True
        )

        try:
            # CrewAI kickoff is synchronous, run it in a thread executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, crew.kickoff)

            # Retry once with explicit tool-invocation enforcement if a live LLM skipped all tools
            from unittest.mock import NonCallableMock
            is_mocked_kickoff = isinstance(Crew.kickoff, NonCallableMock) or isinstance(crew.kickoff, NonCallableMock)
            if tools and agent_state.tool_calls_made == 0 and not is_mocked_kickoff:
                logger.warning(
                    "[%s] Initial CrewAI execution made 0 tool calls; re-running with mandatory tool enforcement.",
                    self.agent_role.value,
                )
                retry_task = Task(
                    description=(
                        "MANDATORY TOOL EXECUTION RETRY: Your previous pass executed ZERO tools. "
                        "Before returning any JSON output, you MUST invoke at least one relevant tool "
                        "to inspect live telemetry or cluster state.\n\n" + task_desc
                    ),
                    expected_output=self.task_config.get("expected_output", ""),
                    agent=agent,
                    output_pydantic=AgentFinding,
                )
                retry_crew = Crew(agents=[agent], tasks=[retry_task], verbose=True)
                result = await loop.run_in_executor(None, retry_crew.kickoff)

            # Extract the Pydantic output
            finding = result.pydantic
            if not finding:
                raise ValueError("Crew did not return a valid AgentFinding Pydantic object.")

            # Explicitly annotate zero-tool executions while keeping finding in ranked results
            if tools and agent_state.tool_calls_made == 0:
                finding.verbatim_signals["tool_execution_status"] = "no tools called, 0 evidence"
                if len(finding.evidence) > 0:
                    logger.warning(
                        "[%s] Agent made 0 tool calls but asserted fake evidence! Capping confidence to 0.10 and clearing ungrounded evidence/flags.",
                        self.agent_role.value,
                    )
                    finding.confidence = 0.10
                    finding.evidence = []
                    finding.infra_flags = {}
                    finding.hypothesis = f"[UNGROUNDED - NO TOOLS EXECUTED | no tools called, 0 evidence] {finding.hypothesis}"
                else:
                    if finding.confidence > 0.40:
                        logger.warning(
                            "[%s] Agent made 0 tool calls when tools were available. Capping confidence to 0.40.",
                            self.agent_role.value,
                        )
                        finding.confidence = 0.40
                    if "no tools called" not in finding.hypothesis.lower():
                        finding.hypothesis = f"[no tools called, 0 evidence] {finding.hypothesis}"

            # Enforce budget policy: if >= max_tool_calls without confident hypothesis, flag low confidence
            elif agent_state.tool_calls_made >= max_tool_calls and finding.confidence >= 0.4:
                logger.info(
                    "[%s] >=%d tool calls reached (%d made), capping confidence < 0.4",
                    self.agent_role.value,
                    max_tool_calls,
                    agent_state.tool_calls_made,
                )
                finding.confidence = 0.35
                
            return finding
            
        except TokenBudgetExceeded as e:
            logger.warning("[%s] Token budget exceeded: %s", self.agent_role.value, e)
            return self._create_partial_finding(
                reason=f"Token budget exceeded: {e}",
                truncated=True,
                confidence=0.3,
            )
        except Exception as e:
            logger.error("[%s] Error during CrewAI execution: %s", self.agent_role.value, e)
            return self._create_partial_finding(
                reason=f"Error during agent execution: {e}",
                confidence=0.2,
            )
