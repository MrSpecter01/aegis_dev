import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import redis

from services.phase2_worker.main import Phase2WorkerService
from services.phase2_worker.graph import create_remediation_graph
from shared.models.actions import ActionProposal, ActionPlanItem
from shared.models.findings import MergedFinding
from tests.helpers.fake_llm import FakeLLM


@pytest.fixture
def mock_redis():
    client = MagicMock(spec=redis.Redis)
    storage = {}

    def mock_get(key):
        return storage.get(key)

    def mock_set(key, val):
        storage[key] = val
        return True

    client.get.side_effect = mock_get
    client.set.side_effect = mock_set
    client.xack.return_value = 1
    client.storage = storage
    return client


@pytest.mark.asyncio
async def test_phase2_full_oom_remediation_lifecycle(mock_redis):
    """
    DoD Test:
    - Phase 2 worker receives job with valid MergedFinding for OOM incident
    - Qdrant returns oom-killed-pod runbook
    - LLM formulates restart_pod action proposal (validating schema & confidence)
    - Action executed and convergence confirmed
    """
    incident_id = "inc-oom-lifecycle-1"

    # 1. Setup Redis state: incident in REMEDIATION with MergedFinding
    merged_finding_payload = {
        "incident_id": incident_id,
        "primary_hypothesis": "Pod cartservice-abc terminated with OOMKilled reason due to memory pressure",
        "merged_confidence": 0.95,
        "verbatim_signals": {"memory": "95%", "exit_code": "137", "reason": "OOMKilled"},
        "all_evidence": [{"source_tool": "k8s_get_events", "raw_output": "OOMKilled pod cartservice-abc"}],
    }
    mock_redis.storage[f"incident:{incident_id}:state"] = json.dumps({
        "incident_id": incident_id,
        "severity": "P2",
        "affected_service": "cartservice",
    })
    mock_redis.storage[f"incident:{incident_id}:context"] = json.dumps(merged_finding_payload)

    # 2. Qdrant returns oom-killed-pod runbook chunk
    qdrant_fixture = {
        "results": [
            {
                "runbook_id": "oom-killed-pod",
                "section": "Remediation Steps",
                "symptom": "OOMKilled pods, memory saturation above 90%",
                "allowed_actions": ["restart_pod", "rollback_release", "update_resource_limits"],
                "content": "1. Restart pod to clear immediate OOM state: `restart_pod`",
            }
        ]
    }
    mock_qdrant = AsyncMock(return_value=qdrant_fixture)

    # 3. LLM formulates restart_pod action proposal
    proposal = ActionProposal(
        incident_id=incident_id,
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice-abc",
                parameters={"namespace": "boutique", "pod_name": "cartservice-abc"},
                rationale="Restart pod to clear immediate OOM state per oom-killed-pod runbook",
                pre_approved=False,
            )
        ],
        confidence=0.92,
        risk_assessment="Low risk restart",
    )
    fake_llm = FakeLLM(response_text=proposal.model_dump_json())

    # 4. Tool execution and convergence mocks
    mock_executor = AsyncMock(return_value={"status": "pod restarted"})
    mock_convergence = AsyncMock(return_value=(True, {}))

    worker = Phase2WorkerService(redis_client=mock_redis)
    worker._post_transition = AsyncMock(return_value=True)

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
        transition_fn=worker._post_transition,
    )
    worker.graph = graph

    # 5. Process Phase 2 job (Step A: Formulate fix & propose action)
    await worker.process_job("msg-1", {"incident_id": incident_id, "retry_count": 0})

    # Assertions for Step A:
    # A. Proposal saved in Redis
    assert f"incident:{incident_id}:proposal" in mock_redis.storage
    saved_action = json.loads(mock_redis.storage[f"incident:{incident_id}:proposal"])
    assert saved_action["actions"][0]["tool_name"] == "restart_pod"
    assert saved_action["confidence"] == 0.92

    # B. Transition action_proposed triggered
    assert worker._post_transition.called
    events = [call[0][1] for call in worker._post_transition.call_args_list]
    assert "action_proposed" in events

    # Step B: Operator approves proposal -> Worker executes action and monitors convergence
    await worker.process_job("msg-2", {"incident_id": incident_id, "action_id": "act-oom-1", "approval_status": "approved", "retry_count": 0})

    events_after_approval = [call[0][1] for call in worker._post_transition.call_args_list]
    assert "execution_success" in events_after_approval
    assert "signals_converged" in events_after_approval

    # C. Execution & convergence confirmed
    mock_executor.assert_called_once_with(
        "restart_pod",
        {"namespace": "boutique", "pod_name": "cartservice-abc", "incident_id": incident_id, "action_id": "act-oom-1"},
    )
    mock_convergence.assert_called_once()


@pytest.mark.asyncio
async def test_phase2_retry_incorporates_prior_attempts_context(mock_redis):
    """
    DoD Test:
    - On retry: prior_attempts is present in the LLM's context
    - LLM proposes an escalated action (update_resource_limits) instead of repeating restart_pod
    """
    incident_id = "inc-oom-retry-2"

    # Setup incident state with prior failed attempt
    prior_attempts = [
        {
            "attempt": 1,
            "action": {
                "actions": [{"tool_name": "restart_pod", "target_resource": "boutique/cartservice-abc"}],
                "confidence": 0.90,
            },
            "outcome": "signals_not_converged",
            "signals_at_timeout": {"memory": "94%", "error_rate": "0.04"},
        }
    ]
    mock_redis.storage[f"incident:{incident_id}:state"] = json.dumps({
        "incident_id": incident_id,
        "severity": "P2",
        "affected_service": "cartservice",
    })
    mock_redis.storage[f"incident:{incident_id}:context"] = json.dumps({
        "incident_id": incident_id,
        "primary_hypothesis": "OOM recurring after pod restart",
        "verbatim_signals": {"memory": "94%"},
    })
    mock_redis.storage[f"incident:{incident_id}:prior_attempts"] = json.dumps(prior_attempts)

    captured_prompt = None

    class InspectingFakeLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            nonlocal captured_prompt
            captured_prompt = str(messages)
            # Escalated action: update_resource_limits
            escalated_proposal = ActionProposal(
                incident_id=incident_id,
                actions=[
                    ActionPlanItem(
                        tool_name="update_resource_limits",
                        target_resource="boutique/cartservice",
                        parameters={"namespace": "boutique", "deployment": "cartservice", "limits": {"memory": "512Mi"}},
                        rationale="Increase memory limit because prior pod restart failed to converge",
                    )
                ],
                confidence=0.88,
                risk_assessment="Medium risk config change",
            )
            return MagicMock(content=escalated_proposal.model_dump_json())

    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock(return_value={"status": "limits updated"})
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        llm=InspectingFakeLLM(),
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
    )

    worker = Phase2WorkerService(redis_client=mock_redis, graph=graph)
    worker._post_transition = AsyncMock(return_value=True)

    # Process retry job with approved execution
    await worker.process_job("msg-2", {"incident_id": incident_id, "action_id": "act-retry-2", "approval_status": "approved", "retry_count": 1})

    # Assertions
    # 1. Prior attempts was present in prompt
    assert captured_prompt is not None
    assert "signals_not_converged" in captured_prompt
    assert "restart_pod" in captured_prompt

    # 2. New action proposal uses update_resource_limits
    assert f"incident:{incident_id}:proposal" in mock_redis.storage
    saved_action = json.loads(mock_redis.storage[f"incident:{incident_id}:proposal"])
    assert saved_action["actions"][0]["tool_name"] == "update_resource_limits"

    # 3. Execution called update_resource_limits
    mock_executor.assert_called_once_with(
        "update_resource_limits",
        {"namespace": "boutique", "deployment": "cartservice", "limits": {"memory": "512Mi"}, "incident_id": incident_id, "action_id": "act-retry-2"},
    )


@pytest.mark.asyncio
async def test_phase2_worker_steer_clears_interrupted_checkpoint_and_replans(mock_redis):
    """After MemorySaver interrupts at request_approval, steering in REMEDIATION must clear checkpoint and re-run formulate_fix."""
    incident_id = "inc-steer-ckpt-1"
    mock_redis.storage[f"incident:{incident_id}:state"] = json.dumps({
        "incident_id": incident_id,
        "state": "REMEDIATION",
        "severity": "P2",
        "affected_service": "cartservice",
        "affected_namespace": "boutique",
    })
    mock_redis.storage[f"incident:{incident_id}:context"] = json.dumps({
        "incident_id": incident_id,
        "primary_hypothesis": "Cartservice high latency",
        "verbatim_signals": {},
    })

    seen_prompts = []
    prop1 = ActionProposal(
        incident_id=incident_id,
        actions=[ActionPlanItem(tool_name="restart_pod", target_resource="boutique/cartservice", parameters={"namespace": "boutique", "pod_name": "cartservice-1"}, rationale="Restart")],
        confidence=0.88,
        risk_assessment="Low",
    )
    prop2 = ActionProposal(
        incident_id=incident_id,
        actions=[ActionPlanItem(tool_name="scale_deployment", target_resource="boutique/cartservice", parameters={"namespace": "boutique", "deployment": "cartservice", "replicas": 3}, rationale="Scale per operator steering")],
        confidence=0.91,
        risk_assessment="Low",
    )

    class TwoPassSteeringLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            seen_prompts.append(str(messages))
            if len(seen_prompts) == 1:
                return MagicMock(content=prop1.model_dump_json())
            return MagicMock(content=prop2.model_dump_json())

    from langgraph.checkpoint.memory import MemorySaver
    saver = MemorySaver()
    worker = Phase2WorkerService(redis_client=mock_redis)
    worker.checkpointer = saver
    worker.graph = create_remediation_graph(
        llm=TwoPassSteeringLLM(),
        qdrant_fn=AsyncMock(return_value={"results": []}),
        checkpointer=saver,
        redis_client=mock_redis,
        transition_fn=AsyncMock(return_value=True),
    )

    # Pass 1: formulate_fix -> request_approval interrupt -> action_proposed
    await worker.process_job("msg-1", {"incident_id": incident_id, "retry_count": 0})
    assert json.loads(mock_redis.storage[f"incident:{incident_id}:proposal"])["actions"][0]["tool_name"] == "restart_pod"

    # Steer back to REMEDIATION
    state_obj = json.loads(mock_redis.storage[f"incident:{incident_id}:state"])
    state_obj["state"] = "REMEDIATION"
    mock_redis.storage[f"incident:{incident_id}:state"] = json.dumps(state_obj)
    mock_redis.storage[f"incident:{incident_id}:steering"] = "Scale replicas to 3 instead of restarting"

    # Pass 2: must NOT get trapped at interrupted request_approval; must re-enter formulate_fix
    await worker.process_job("msg-2", {"incident_id": incident_id, "retry_count": 0})
    assert len(seen_prompts) == 2
    assert "Scale replicas to 3 instead of restarting" in seen_prompts[1]
    assert json.loads(mock_redis.storage[f"incident:{incident_id}:proposal"])["actions"][0]["tool_name"] == "scale_deployment"
