"""Unit tests for Phase 1 specialist investigator agents."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.phase1_worker.agents.base import BaseInvestigator
from services.phase1_worker.agents.change_config import ChangeConfigInvestigator
from services.phase1_worker.agents.infrastructure import InfrastructureInvestigator
from services.phase1_worker.agents.logs_traces import LogTraceInvestigator
from shared.harness.llm import TokenBudgetExceeded
from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding


class MockCrewOutput:
    def __init__(self, pydantic_obj):
        self.pydantic = pydantic_obj


@pytest.fixture
def mock_crew_kickoff():
    with patch("services.phase1_worker.agents.base.Crew.kickoff") as mock_kickoff:
        yield mock_kickoff


def test_base_investigator_load_yaml_config_errors():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})

    # Missing file
    with pytest.raises(FileNotFoundError, match="Missing config file"):
        investigator._load_yaml_config("nonexistent.yaml", "key")

    # Missing key
    with pytest.raises(KeyError, match="Key 'nonexistent_key' not found"):
        investigator._load_yaml_config("agents.yaml", "nonexistent_key")


def test_base_investigator_create_partial_finding():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})
    finding = investigator._create_partial_finding(
        reason="Budget exceeded",
        truncated=True,
        confidence=0.25,
        infra_flags={"oom_killed": True},
    )
    assert finding.agent_role == AgentRole.INFRASTRUCTURE_INVESTIGATOR
    assert finding.hypothesis == "Budget exceeded"
    assert finding.truncated is True
    assert finding.confidence == 0.25
    assert finding.infra_flags["oom_killed"] is True


@pytest.mark.asyncio
async def test_base_investigator_execute_crew_no_llm_raises():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})
    investigator.llm = None
    with pytest.raises(ValueError, match="No LLM configured for investigator"):
        await investigator._execute_crew({})


def test_base_investigator_auto_wire_llm_and_mcp():
    investigator = InfrastructureInvestigator()
    assert investigator.llm is not None
    assert "prometheus" in investigator.mcp_clients
    assert "kubernetes" in investigator.mcp_clients


def test_base_investigator_budget_hierarchy():
    # Constructor takes top precedence
    inv1 = InfrastructureInvestigator(token_budget=5555, max_iter=7, max_tool_calls=4)
    assert inv1.token_budget == 5555
    assert inv1.max_iter == 7
    assert inv1.max_tool_calls == 4

    # Default to prompt config (10000 / 10 / 10)
    inv2 = InfrastructureInvestigator(llm="fake", mcp_clients={})
    assert inv2.token_budget == 10000
    assert inv2.max_iter == 10
    assert inv2.max_tool_calls == 10


def test_base_investigator_prompts_dir_override(tmp_path):
    with pytest.raises(FileNotFoundError):
        InfrastructureInvestigator(prompts_dir=tmp_path)


def test_base_investigator_load_yaml_config_success():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})
    data = investigator._load_yaml_config("agents.yaml", "infrastructure_investigator")
    assert data["role"] == "Infrastructure Investigator"


def test_base_investigator_auto_wire_exceptions_handled():
    with patch("shared.harness.llm.build_llm", side_effect=RuntimeError("LLM fail")):
        inv = InfrastructureInvestigator(mcp_clients={})
        assert inv.llm is None

    with patch("shared.harness.mcp.get_mcp_clients", side_effect=RuntimeError("MCP fail")):
        inv = InfrastructureInvestigator(llm="fake")
        assert inv.mcp_clients == {}

    with patch("shared.config.loader.load_agents_config", side_effect=RuntimeError("Config fail")):
        inv = InfrastructureInvestigator(llm="fake", mcp_clients={})
        assert inv.token_budget == 10000




@pytest.mark.asyncio
async def test_base_investigator_execute_crew_token_budget_exceeded():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})
    with patch("services.phase1_worker.agents.base.Crew.kickoff", side_effect=TokenBudgetExceeded("Out of tokens", 11000, 10000)):
        finding = await investigator._execute_crew({})
        assert finding.truncated is True
        assert "Token budget exceeded" in finding.hypothesis
        assert finding.confidence == 0.3


@pytest.mark.asyncio
async def test_base_investigator_execute_crew_generic_exception():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})
    with patch("services.phase1_worker.agents.base.Crew.kickoff", side_effect=RuntimeError("Unexpected crash")):
        finding = await investigator._execute_crew({})
        assert finding.truncated is False
        assert "Error during agent execution: Unexpected crash" in finding.hypothesis
        assert finding.confidence == 0.2


@pytest.mark.asyncio
async def test_base_investigator_execute_crew_pydantic_none():
    investigator = InfrastructureInvestigator(llm="fake", mcp_clients={})
    with patch("services.phase1_worker.agents.base.Crew.kickoff", return_value=MockCrewOutput(None)):
        finding = await investigator._execute_crew({})
        assert finding.truncated is False
        assert "Crew did not return a valid AgentFinding Pydantic object" in finding.hypothesis
        assert finding.confidence == 0.2


@pytest.mark.asyncio
async def test_base_investigator_execute_crew_llm_callbacks_handling():
    sample_finding = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="Callbacks test",
        confidence=0.8,
        evidence=[],
        verbatim_signals={},
    )
    with patch("services.phase1_worker.agents.base.Agent"), \
         patch("services.phase1_worker.agents.base.Task"), \
         patch("services.phase1_worker.agents.base.Crew") as mock_crew_cls:
        mock_crew = MagicMock()
        mock_crew.kickoff.return_value = MockCrewOutput(sample_finding)
        mock_crew_cls.return_value = mock_crew

        # 1. LLM with callbacks = None
        mock_llm1 = MagicMock()
        mock_llm1.callbacks = None
        inv1 = InfrastructureInvestigator(llm=mock_llm1, mcp_clients={})
        await inv1._execute_crew({})
        assert len(mock_llm1.callbacks) == 1

        # 2. LLM with callbacks = []
        mock_llm2 = MagicMock()
        mock_llm2.callbacks = []
        inv2 = InfrastructureInvestigator(llm=mock_llm2, mcp_clients={})
        await inv2._execute_crew({})
        assert len(mock_llm2.callbacks) == 1


@pytest.mark.asyncio
async def test_base_investigator_execute_crew_allowed_tools_filter():
    from crewai.tools import BaseTool

    class DummyTool(BaseTool):
        name: str
        description: str = "dummy"

        def _run(self, *args, **kwargs):
            return "ok"

    mock_client = MagicMock()
    mock_tool1 = DummyTool(name="k8s_get_pod_status")
    mock_tool2 = DummyTool(name="unwanted_tool")

    with patch("services.phase1_worker.agents.base.build_mcp_tools", AsyncMock(return_value=[mock_tool1, mock_tool2])), \
         patch("services.phase1_worker.agents.base.Crew") as mock_crew_cls, \
         patch("services.phase1_worker.agents.base.Crew.kickoff") as mock_kickoff:
        sample_finding = AgentFinding(
            agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
            hypothesis="Filtered tool test",
            confidence=0.8,
            evidence=[],
            verbatim_signals={},
            infra_flags={},
            recommended_actions=[],
            truncated=False,
            timestamp=datetime.now(timezone.utc),
        )
        mock_crew_cls.kickoff = mock_kickoff
        mock_crew_cls.return_value.kickoff = mock_kickoff
        mock_kickoff.return_value = MockCrewOutput(sample_finding)

        investigator = InfrastructureInvestigator(llm="fake", mcp_clients={"k8s": mock_client})
        finding = await investigator._execute_crew({}, allowed_tools=["k8s_get_pod_status"])
        assert "Filtered tool test" in finding.hypothesis
        assert "no tools called, 0 evidence" in finding.hypothesis
        assert finding.verbatim_signals.get("tool_execution_status") == "no tools called, 0 evidence"
        assert finding.confidence == 0.40


@pytest.mark.asyncio
async def test_infra_investigator_success(mock_crew_kickoff):
    finding = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="Pod in CrashLoopBackOff due to OOMKilled",
        confidence=0.88,
        evidence=[],
        verbatim_signals={"exit_code": 137},
        infra_flags={"oom_killed": True, "node_eviction": False},
        recommended_actions=["restart_pod"],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    mock_redis = MagicMock()

    investigator = InfrastructureInvestigator(
        llm="fake",
        mcp_clients={},
        redis_client=mock_redis,
    )

    result = await investigator.investigate(
        incident_id="inc-test-01",
        affected_namespace="boutique",
        affected_service="cartservice",
    )

    assert isinstance(result, AgentFinding)
    assert result.agent_role == AgentRole.INFRASTRUCTURE_INVESTIGATOR
    assert result.confidence == 0.88
    assert result.infra_flags["oom_killed"] is True

    # Verify infra_flags were persisted to Redis
    mock_redis.set.assert_called_once()
    saved_key = mock_redis.set.call_args[0][0]
    assert saved_key == "incident:inc-test-01:flags"


@pytest.mark.asyncio
async def test_infra_investigator_redis_failure_handled(mock_crew_kickoff):
    finding = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="Pod OOMKilled",
        confidence=0.85,
        evidence=[],
        verbatim_signals={},
        infra_flags={"oom_killed": True},
        recommended_actions=[],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    mock_redis = MagicMock()
    mock_redis.set.side_effect = RuntimeError("Redis connection lost")

    investigator = InfrastructureInvestigator(
        llm="fake",
        mcp_clients={},
        redis_client=mock_redis,
    )

    result = await investigator.investigate(incident_id="inc-test-err")
    assert result.confidence == 0.85


@pytest.mark.asyncio
async def test_logs_investigator_shallow_mode(caplog, mock_crew_kickoff):
    caplog.set_level(logging.INFO)
    finding = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Corroborated OOMKilled log pattern",
        confidence=0.85,
        evidence=[],
        verbatim_signals={"error_type": "OutOfMemoryError"},
        infra_flags={},
        recommended_actions=[],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    investigator = LogTraceInvestigator(
        llm="fake",
        mcp_clients={},
    )

    result = await investigator.investigate(
        incident_id="inc-log-01",
        affected_namespace="boutique",
        affected_service="cartservice",
        infra_flags={"oom_killed": True},
    )

    assert result.agent_role == AgentRole.LOG_TRACE_INVESTIGATOR
    assert result.confidence == 0.85
    assert "search_depth=shallow (guided)" in caplog.text


@pytest.mark.asyncio
async def test_logs_investigator_shallow_mode_on_node_eviction(caplog, mock_crew_kickoff):
    caplog.set_level(logging.INFO)
    finding = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Node evicted",
        confidence=0.8,
        evidence=[],
        verbatim_signals={},
        infra_flags={},
        recommended_actions=[],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    investigator = LogTraceInvestigator(llm="fake", mcp_clients={})
    await investigator.investigate(
        incident_id="inc-log-evict",
        infra_flags={"node_eviction": True},
    )
    assert "search_depth=shallow (guided)" in caplog.text


@pytest.mark.asyncio
async def test_logs_investigator_deep_mode(caplog, mock_crew_kickoff):
    caplog.set_level(logging.INFO)
    finding = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Deep log scan",
        confidence=0.90,
        evidence=[],
        verbatim_signals={},
        infra_flags={},
        recommended_actions=[],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    investigator = LogTraceInvestigator(
        llm="fake",
        mcp_clients={},
    )

    result = await investigator.investigate(
        incident_id="inc-log-02",
        affected_namespace="boutique",
        affected_service="cartservice",
        infra_flags={"oom_killed": False},
    )

    assert result.agent_role == AgentRole.LOG_TRACE_INVESTIGATOR
    assert "search_depth=deep (guided)" in caplog.text


def test_logs_investigator_read_infra_flags_from_redis():
    mock_redis = MagicMock()
    import redis
    mock_redis.__class__ = redis.Redis

    # 1. Valid flags
    mock_redis.get.return_value = json.dumps({"oom_killed": True})
    investigator = LogTraceInvestigator(llm="fake", redis_client=mock_redis)
    flags = investigator._read_infra_flags("inc-redis-01")
    assert flags == {"oom_killed": True}

    # 2. Redis returns None
    mock_redis.get.return_value = None
    flags = investigator._read_infra_flags("inc-redis-02")
    assert flags == {}

    # 3. Redis raises exception
    mock_redis.get.side_effect = RuntimeError("Redis error")
    flags = investigator._read_infra_flags("inc-redis-03")
    assert flags == {}

    # 4. No redis client
    investigator_no_redis = LogTraceInvestigator(llm="fake", redis_client=None)
    assert investigator_no_redis._read_infra_flags("inc-none") == {}


@pytest.mark.asyncio
async def test_change_config_investigator_success(mock_crew_kickoff):
    finding = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Recent ArgoCD sync revision",
        confidence=0.82,
        evidence=[],
        verbatim_signals={"revision": "v1.2.3"},
        infra_flags={},
        recommended_actions=["rollback_release"],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    investigator = ChangeConfigInvestigator(
        llm="fake",
        mcp_clients={},
    )

    result = await investigator.investigate(
        incident_id="inc-change-01",
        affected_namespace="boutique",
        affected_service="cartservice",
        alert_timestamp=datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert result.agent_role == AgentRole.CHANGE_CONFIG_INVESTIGATOR
    assert result.confidence == 0.82
    assert "rollback_release" in result.recommended_actions


@pytest.mark.asyncio
async def test_change_config_investigator_default_timestamp(mock_crew_kickoff):
    finding = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Default timestamp test",
        confidence=0.75,
        evidence=[],
        verbatim_signals={},
        infra_flags={},
        recommended_actions=[],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )
    mock_crew_kickoff.return_value = MockCrewOutput(finding)

    investigator = ChangeConfigInvestigator(llm="fake", mcp_clients={})
    result = await investigator.investigate(
        incident_id="inc-change-now",
        alert_timestamp=None,  # Tests datetime.now(timezone.utc) path
    )
    assert result.confidence == 0.75


@pytest.mark.asyncio
async def test_tool_calls_counter_budget_policy():
    """If >= max_tool_calls (10) without confident hypothesis, confidence is capped < 0.4."""
    from shared.harness.crewai_tools import AgentBudgetState

    finding = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="Inconclusive",
        confidence=0.85,
        evidence=[],
        verbatim_signals={},
        infra_flags={},
        recommended_actions=[],
        truncated=False,
        timestamp=datetime.now(timezone.utc),
    )

    with patch("services.phase1_worker.agents.base.Crew.kickoff") as mock_kickoff, \
         patch("services.phase1_worker.agents.base.AgentBudgetState") as mock_state_cls:

        mock_kickoff.return_value = MockCrewOutput(finding)

        # Mock the state object to have 10 tool calls (matching D54 max_tool_calls=10)
        mock_state_instance = MagicMock(spec=AgentBudgetState)
        mock_state_instance.tool_calls_made = 10
        mock_state_cls.return_value = mock_state_instance

        investigator = InfrastructureInvestigator(
            llm="fake",
            mcp_clients={},
        )

        result = await investigator.investigate(
            incident_id="inc-test-02",
        )

        assert result.confidence == 0.35


@pytest.mark.asyncio
async def test_base_investigator_mandatory_tool_retry_executes_tools():
    """Live callable Crew.kickoff with 0 tool calls on pass 1 triggers a mandatory retry."""
    from crewai.tools import BaseTool
    from shared.models.findings import EvidenceItem

    class CountingTool(BaseTool):
        name: str = "k8s_get_pod_status"
        description: str = "dummy"

        def _run(self, *args, **kwargs):
            return "ok"

    call_attempts = []
    fake_state = MagicMock()
    fake_state.tool_calls_made = 0

    class FakeCrew:
        def __init__(self, agents=None, tasks=None, verbose=True):
            self.tasks = tasks or []

        def kickoff(self):
            attempt = len(call_attempts) + 1
            call_attempts.append(self.tasks[0].description if self.tasks else "")
            if attempt == 1:
                fake_state.tool_calls_made = 0
                return MockCrewOutput(
                    AgentFinding(
                        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
                        hypothesis="Initial guess without tools",
                        confidence=0.7,
                        evidence=[],
                    )
                )
            fake_state.tool_calls_made = 1
            return MockCrewOutput(
                AgentFinding(
                    agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
                    hypothesis="Confirmed OOMKilled via pod status",
                    confidence=0.92,
                    evidence=[
                        EvidenceItem(
                            source_tool="k8s_get_pod_status",
                            query_or_cmd="k8s_get_pod_status(pod='cart')",
                            raw_output="OOMKilled exit 137",
                        )
                    ],
                )
            )

    with patch("services.phase1_worker.agents.base.build_mcp_tools", AsyncMock(return_value=[CountingTool()])), \
         patch("services.phase1_worker.agents.base.AgentBudgetState", return_value=fake_state), \
         patch("services.phase1_worker.agents.base.Crew", FakeCrew):
        investigator = InfrastructureInvestigator(llm="fake", mcp_clients={"k8s": MagicMock()})
        finding = await investigator._execute_crew({})

    assert len(call_attempts) == 2
    assert "MANDATORY TOOL EXECUTION RETRY" in call_attempts[1]
    assert finding.hypothesis == "Confirmed OOMKilled via pod status"
    assert finding.confidence == 0.92


def test_langchain_to_crew_llm_forwards_temp_and_retries_429():
    """Verify _langchain_to_crew_llm forwards temperature/max_tokens and CrewLLMWithFallbacks backs off on 429."""
    from services.phase1_worker.agents.base import CrewLLMWithFallbacks, _langchain_to_crew_llm

    class FakeLangChainGoogleModel:
        __module__ = "langchain_google_genai.chat_models"
        model_name = "gemini-2.5-flash"
        temperature = 0.05
        max_output_tokens = 2048

    fake_lc = FakeLangChainGoogleModel()

    captured_kwargs = {}

    class StubCrewLLM:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    with patch("crewai.LLM", StubCrewLLM):
        _langchain_to_crew_llm(fake_lc)

    assert captured_kwargs.get("temperature") == 0.05
    assert captured_kwargs.get("max_tokens") == 2048

    attempts = {"count": 0}

    class FlakyPrimary:
        model = "gemini/gemini-2.5-flash"

        def call(self, messages, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED transient rate limit")
            return '{"ok": true}'

    wrapper = CrewLLMWithFallbacks(
        primary=FlakyPrimary(),
        fallbacks=[],
        model="gemini/gemini-2.5-flash",
    )
    with patch("services.phase1_worker.agents.base.time.sleep") as mock_sleep:
        out = wrapper.call([{"role": "user", "content": "hi"}])

    assert out == '{"ok": true}'
    assert attempts["count"] == 2
    mock_sleep.assert_called_once()
