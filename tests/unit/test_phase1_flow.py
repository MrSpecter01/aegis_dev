"""Unit tests for Phase 1 Flow orchestration and worker service.

Zero external dependencies: uses mocked agents, in-memory/mocked Redis, and mock HTTP API.
"""

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.phase1_worker.flow import Phase1Flow
from services.phase1_worker.main import Phase1WorkerService
from shared.models.enums import AgentRole
from shared.models.findings import AgentFinding, EvidenceItem, MergedFinding


def _make_sample_finding(role: AgentRole, confidence: float, hypothesis: str, truncated: bool = False) -> AgentFinding:
    return AgentFinding(
        agent_role=role,
        hypothesis=hypothesis,
        confidence=confidence,
        evidence=[
            EvidenceItem(
                source_tool="test_tool",
                query_or_cmd="test query",
                raw_output=f"raw output for {role.value}",
            )
        ],
        infra_flags={"oom_killed": True} if role == AgentRole.INFRASTRUCTURE_INVESTIGATOR else {},
        verbatim_signals={"role": role.value},
        truncated=truncated,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_phase1_flow_orchestration_and_redis_save():
    mock_redis = MagicMock()
    mock_redis.set = MagicMock()

    mock_a1 = MagicMock()
    mock_a1.investigate = AsyncMock(
        return_value=_make_sample_finding(AgentRole.INFRASTRUCTURE_INVESTIGATOR, 0.85, "Pod OOMKilled")
    )

    mock_a2 = MagicMock()
    mock_a2.investigate = AsyncMock(
        return_value=_make_sample_finding(AgentRole.LOG_TRACE_INVESTIGATOR, 0.90, "OutOfMemoryError in cartservice")
    )

    mock_a3 = MagicMock()
    mock_a3.investigate = AsyncMock(
        return_value=_make_sample_finding(AgentRole.CHANGE_CONFIG_INVESTIGATOR, 0.70, "No recent changes")
    )

    flow = Phase1Flow(
        incident_id="inc-flow-01",
        affected_namespace="boutique",
        affected_service="cartservice",
        infra_investigator=mock_a1,
        log_investigator=mock_a2,
        change_investigator=mock_a3,
        redis_client=mock_redis,
    )

    merged = await flow.run()

    # 1. Verify all three agents were called
    mock_a1.investigate.assert_called_once()
    mock_a2.investigate.assert_called_once()
    mock_a3.investigate.assert_called_once()

    # 2. Verify returned finding
    assert isinstance(merged, MergedFinding)
    assert merged.incident_id == "inc-flow-01"
    assert len(merged.all_evidence) == 3
    assert merged.findings_by_agent[AgentRole.INFRASTRUCTURE_INVESTIGATOR].confidence == 0.85
    assert merged.findings_by_agent[AgentRole.LOG_TRACE_INVESTIGATOR].confidence == 0.90

    # 3. Verify MergedFinding written to incident:{id}:context in Redis (DoD item 6)
    context_calls = [
        call for call in mock_redis.set.call_args_list
        if call[0][0] == "incident:inc-flow-01:context"
    ]
    assert len(context_calls) == 1
    saved_json = context_calls[0][0][1]
    parsed_saved = json.loads(saved_json)
    assert parsed_saved["incident_id"] == "inc-flow-01"


@pytest.mark.asyncio
async def test_phase1_flow_with_token_truncation_advances():
    """Token budget exceeded on an agent -> flow completes, doesn't crash, still merges."""
    mock_redis = MagicMock()

    # Agent 1 was truncated
    mock_a1 = MagicMock()
    mock_a1.investigate = AsyncMock(
        return_value=_make_sample_finding(
            AgentRole.INFRASTRUCTURE_INVESTIGATOR, 0.3, "Truncated investigation", truncated=True
        )
    )

    mock_a2 = MagicMock()
    mock_a2.investigate = AsyncMock(
        return_value=_make_sample_finding(AgentRole.LOG_TRACE_INVESTIGATOR, 0.85, "Logs normal")
    )

    mock_a3 = MagicMock()
    mock_a3.investigate = AsyncMock(
        return_value=_make_sample_finding(AgentRole.CHANGE_CONFIG_INVESTIGATOR, 0.75, "No changes")
    )

    flow = Phase1Flow(
        incident_id="inc-flow-trunc",
        affected_namespace="boutique",
        affected_service="cartservice",
        infra_investigator=mock_a1,
        log_investigator=mock_a2,
        change_investigator=mock_a3,
        redis_client=mock_redis,
    )

    merged = await flow.run()

    assert isinstance(merged, MergedFinding)
    assert merged.findings_by_agent[AgentRole.INFRASTRUCTURE_INVESTIGATOR].truncated is True
    # Primary hypothesis is picked from non-truncated higher confidence agent (Agent 2)
    assert merged.primary_hypothesis == "Logs normal"


@pytest.mark.asyncio
async def test_phase1_worker_service_job_processing():
    mock_redis = MagicMock()
    mock_redis.xack = MagicMock()

    worker = Phase1WorkerService(
        redis_client=mock_redis,
        api_url="http://test-api:8080",
        api_key="test-key",
    )

    # Mock the transition HTTP posts
    transition_calls = []

    async def mock_post_transition(incident_id, event, metadata=None):
        transition_calls.append((incident_id, event, metadata))
        return True

    worker._post_transition = mock_post_transition

    # Mock Phase1Flow execution
    sample_merged = MergedFinding(
        incident_id="inc-job-01",
        primary_hypothesis="OOMKilled verified",
        merged_confidence=0.88,
        findings_by_agent={},
        all_evidence=[],
        verbatim_signals={},
        infra_flags={"oom_killed": True},
        corroboration_score=0.9,
    )

    with patch("services.phase1_worker.main.Phase1Flow") as MockFlow:
        mock_flow_instance = MagicMock()
        mock_flow_instance.run = AsyncMock(return_value=sample_merged)
        MockFlow.return_value = mock_flow_instance

        job_data = {
            "incident_id": "inc-job-01",
            "affected_service": "cartservice",
            "affected_namespace": "boutique",
        }

        await worker.process_job("msg-101", job_data)

    # Verify transitions called: triage_complete then merge_complete
    assert len(transition_calls) == 2
    assert transition_calls[0] == ("inc-job-01", "triage_complete", None)
    assert transition_calls[1][0] == "inc-job-01"
    assert transition_calls[1][1] == "merge_complete"
    assert transition_calls[1][2]["merged_confidence"] == 0.88

    # Verify Redis stream ACK was called
    mock_redis.xack.assert_called_once_with("stream:phase1:jobs", "phase1-workers", "msg-101")


@pytest.mark.asyncio
async def test_phase1_flow_without_redis_client():
    mock_a1 = MagicMock()
    mock_a1.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.INFRASTRUCTURE_INVESTIGATOR, 0.8, "A1"))
    mock_a2 = MagicMock()
    mock_a2.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.LOG_TRACE_INVESTIGATOR, 0.8, "A2"))
    mock_a3 = MagicMock()
    mock_a3.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.CHANGE_CONFIG_INVESTIGATOR, 0.8, "A3"))

    flow = Phase1Flow(
        incident_id="inc-no-redis",
        infra_investigator=mock_a1,
        log_investigator=mock_a2,
        change_investigator=mock_a3,
        redis_client=None,
    )
    merged = await flow.run()
    assert merged.incident_id == "inc-no-redis"


@pytest.mark.asyncio
async def test_phase1_flow_redis_write_exceptions_handled():
    mock_redis = MagicMock()
    mock_redis.set.side_effect = RuntimeError("Redis write error")

    mock_a1 = MagicMock()
    mock_a1.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.INFRASTRUCTURE_INVESTIGATOR, 0.8, "A1"))
    mock_a2 = MagicMock()
    mock_a2.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.LOG_TRACE_INVESTIGATOR, 0.8, "A2"))
    mock_a3 = MagicMock()
    mock_a3.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.CHANGE_CONFIG_INVESTIGATOR, 0.8, "A3"))

    flow = Phase1Flow(
        incident_id="inc-redis-err",
        infra_investigator=mock_a1,
        log_investigator=mock_a2,
        change_investigator=mock_a3,
        redis_client=mock_redis,
    )
    # Should complete without crashing even if redis.set throws
    merged = await flow.run()
    assert merged.incident_id == "inc-redis-err"


@pytest.mark.asyncio
async def test_phase1_flow_parallel_investigators_with_none_a1():
    mock_a2 = MagicMock()
    mock_a2.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.LOG_TRACE_INVESTIGATOR, 0.8, "A2"))
    mock_a3 = MagicMock()
    mock_a3.investigate = AsyncMock(return_value=_make_sample_finding(AgentRole.CHANGE_CONFIG_INVESTIGATOR, 0.8, "A3"))

    flow = Phase1Flow(
        incident_id="inc-none-a1",
        log_investigator=mock_a2,
        change_investigator=mock_a3,
    )
    # Call run_parallel_investigators directly with a1_finding=None
    a2, a3 = await flow.run_parallel_investigators(None)
    assert a2.agent_role == AgentRole.LOG_TRACE_INVESTIGATOR
    assert a3.agent_role == AgentRole.CHANGE_CONFIG_INVESTIGATOR
    mock_a2.investigate.assert_awaited_once_with(
        incident_id="inc-none-a1",
        affected_namespace="default",
        affected_service="unknown",
        infra_flags={},
        alert_metadata={},
        topology_subgraph={},
    )
