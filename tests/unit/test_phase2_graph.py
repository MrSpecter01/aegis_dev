import json
import pytest
from unittest.mock import AsyncMock

from services.phase2_worker.graph import (
    create_remediation_graph,
    Phase2GraphState,
    classify_live_fault_signature,
    evaluate_pre_execution_gate,
)
from shared.models.actions import ActionProposal, ActionPlanItem
from tests.helpers.fake_llm import FakeLLM


@pytest.fixture
def sample_proposal_json():
    proposal = ActionProposal(
        incident_id="inc-oom-1",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice-123",
                parameters={"namespace": "boutique", "pod_name": "cartservice-123"},
                rationale="Clear memory pressure",
                pre_approved=False,
            )
        ],
        confidence=0.85,
        risk_assessment="Low risk restart",
    )
    return proposal.model_dump_json()


@pytest.mark.asyncio
async def test_graph_low_confidence_escalation(sample_proposal_json):
    # Proposal with confidence 0.65 (< 0.80 for P2)
    low_conf_data = json.loads(sample_proposal_json)
    low_conf_data["confidence"] = 0.65
    low_conf_json = json.dumps(low_conf_data)

    fake_llm = FakeLLM(response_text=low_conf_json)
    mock_qdrant = AsyncMock(return_value={"results": [{"runbook_id": "oom-killed-pod"}]})

    graph = create_remediation_graph(llm=fake_llm, qdrant_fn=mock_qdrant)

    initial_state: Phase2GraphState = {
        "incident_id": "inc-oom-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod in cartservice"},
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["outcome"] == "low_confidence"
    assert "execution_results" not in final_state or not final_state["execution_results"]


@pytest.mark.asyncio
async def test_graph_approved_execution_and_convergence(sample_proposal_json):
    fake_llm = FakeLLM(response_text=sample_proposal_json)
    mock_qdrant = AsyncMock(return_value={"results": [{"runbook_id": "oom-killed-pod"}]})
    mock_executor = AsyncMock(return_value={"status": "restarted"})
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-oom-1",
        "action_id": "act-oom-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod in cartservice"},
        "approval_status": "approved",  # Operator already approved
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["outcome"] == "signals_converged"
    assert len(final_state["execution_results"]) == 1
    assert final_state["execution_results"][0]["status"] == "success"
    mock_executor.assert_called_once()
    mock_convergence.assert_called_once()


@pytest.mark.asyncio
async def test_graph_denied_approval_escalation(sample_proposal_json):
    fake_llm = FakeLLM(response_text=sample_proposal_json)
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock()

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-oom-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod"},
        "approval_status": "denied",  # Operator denied
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["outcome"] == "denied"
    mock_executor.assert_not_called()


@pytest.mark.asyncio
async def test_graph_execution_failure_routing(sample_proposal_json):
    fake_llm = FakeLLM(response_text=sample_proposal_json)
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock(side_effect=RuntimeError("K8s API unreachable"))

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-oom-1",
        "action_id": "act-oom-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod"},
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["outcome"] == "execution_failed"
    assert "K8s API unreachable" in final_state.get("error", "")


@pytest.mark.asyncio
async def test_graph_signals_not_converged_outcome(sample_proposal_json):
    fake_llm = FakeLLM(response_text=sample_proposal_json)
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock(return_value={"status": "restarted"})
    mock_convergence = AsyncMock(return_value=(False, {"cartservice.error_rate": 0.05}))

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-oom-1",
        "action_id": "act-oom-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod"},
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["outcome"] == "signals_not_converged"
    assert final_state["signals_at_timeout"] == {"cartservice.error_rate": 0.05}


@pytest.mark.asyncio
async def test_formulate_fix_hot_injects_rules_and_catalog(sample_proposal_json):
    captured_system_msg = None
    captured_human_msg = None

    class InspectingLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            nonlocal captured_system_msg, captured_human_msg
            for m in messages:
                if hasattr(m, "content"):
                    if "operational_rules" in str(m.content):
                        captured_system_msg = str(m.content)
                    if "runbook_catalog" in str(m.content):
                        captured_human_msg = str(m.content)
            from unittest.mock import MagicMock
            return MagicMock(content=sample_proposal_json)

    custom_index = [
        {
            "id": "pm-dns-rules",
            "title": "DNS Flapping Rules",
            "types": ["postmortem", "rule"],
            "rules": ["Never drain DNS nodes during peak hours without upstream fallback."],
            "symptoms": ["503 timeout"],
            "summary": "CoreDNS postmortem policy",
            "allowed_actions": ["scale_deployment"],
        }
    ]

    graph = create_remediation_graph(llm=InspectingLLM())

    initial_state: Phase2GraphState = {
        "incident_id": "inc-rules-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "CoreDNS 503 errors"},
        "runbook_index": custom_index,
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert captured_system_msg is not None
    assert "<operational_rules>" in captured_system_msg
    assert "Never drain DNS nodes during peak hours" in captured_system_msg

    assert captured_human_msg is not None
    assert "<runbook_catalog>" in captured_human_msg
    assert "pm-dns-rules" in captured_human_msg


@pytest.mark.asyncio
async def test_formulate_fix_react_retrieval_loop():
    from unittest.mock import MagicMock
    from shared.models.actions import ActionProposal, ActionPlanItem

    proposal = ActionProposal(
        incident_id="inc-react-1",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice",
                parameters={"namespace": "boutique", "pod_name": "cartservice-xyz"},
                rationale="Restart pod per dynamically retrieved runbook",
            )
        ],
        confidence=0.91,
        risk_assessment="Low risk restart",
    )

    turn = 0

    class ReActLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            nonlocal turn
            turn += 1
            if turn == 1:
                # First turn: model requests qdrant_search tool call
                mock_msg = MagicMock()
                mock_msg.content = ""
                mock_msg.tool_calls = [
                    {
                        "name": "qdrant_search",
                        "args": {"id": "oom-killed-pod"},
                        "id": "call-1",
                    }
                ]
                return mock_msg
            else:
                # Second turn: receives tool response and outputs proposal
                mock_msg = MagicMock()
                mock_msg.content = proposal.model_dump_json()
                mock_msg.tool_calls = []
                return mock_msg

    mock_qdrant = AsyncMock(return_value={
        "results": [
            {
                "runbook_id": "oom-killed-pod",
                "section": "Remediation Steps",
                "content": "1. Restart pod: `restart_pod`",
            }
        ]
    })

    graph = create_remediation_graph(llm=ReActLLM(), qdrant_fn=mock_qdrant)

    initial_state: Phase2GraphState = {
        "incident_id": "inc-react-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod in cartservice"},
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert mock_qdrant.called
    assert mock_qdrant.call_args.kwargs == {"id": "oom-killed-pod"}
    assert len(final_state.get("retrieved_runbooks", [])) == 1
    assert final_state["retrieved_runbooks"][0]["runbook_id"] == "oom-killed-pod"
    assert final_state["action_proposal"]["actions"][0]["tool_name"] == "restart_pod"


@pytest.mark.asyncio
async def test_formulate_fix_retrieval_budget_tracking():
    from unittest.mock import MagicMock
    from shared.models.actions import ActionProposal, ActionPlanItem
    import json

    proposal = ActionProposal(
        incident_id="inc-budget-1",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice",
                parameters={"namespace": "boutique", "pod_name": "cartservice-xyz"},
                rationale="Restart pod",
            )
        ],
        confidence=0.95,
        risk_assessment="Low",
    )

    captured_messages = []
    turn = 0

    class BudgetTrackingLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            nonlocal turn
            turn += 1
            captured_messages.append(list(messages))
            if turn == 1:
                mock_msg = MagicMock()
                mock_msg.content = ""
                mock_msg.tool_calls = [{"name": "qdrant_search", "args": {"id": "oom-killed-pod"}, "id": "tc-1"}]
                return mock_msg
            else:
                mock_msg = MagicMock()
                mock_msg.content = proposal.model_dump_json()
                mock_msg.tool_calls = []
                return mock_msg

    mock_qdrant = AsyncMock(return_value={"results": [{"runbook_id": "oom-killed-pod"}]})
    graph = create_remediation_graph(llm=BudgetTrackingLLM(), qdrant_fn=mock_qdrant)

    initial_state: Phase2GraphState = {
        "incident_id": "inc-budget-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOM in cartservice"},
    }

    final_state = await graph.ainvoke(initial_state)

    # 1. Verify initial prompt contains <retrieval_budget>
    initial_user_msg = captured_messages[0][1].content
    assert "<retrieval_budget>" in initial_user_msg
    assert "Max allowed retrieval steps: 8" in initial_user_msg

    # 2. Verify second turn messages contain ToolMessage with _budget_status
    second_turn_msgs = captured_messages[1]
    tool_msg = [m for m in second_turn_msgs if hasattr(m, "content") and "_budget_status" in m.content]
    assert len(tool_msg) == 1
    tool_data = json.loads(tool_msg[0].content)
    assert "Step 1 of 8 completed (7 remaining)" in tool_data["_budget_status"]


@pytest.mark.asyncio
async def test_execute_action_node_dynamic_tool_routing():
    from unittest.mock import MagicMock, patch, MagicMock
    from services.phase2_worker.graph import create_remediation_graph

    # Proposal with non-kubernetes tool toggle_feature_flag
    ff_proposal = ActionProposal(
        incident_id="inc-ff-1",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="paymentservice",
                parameters={"flag_name": "new_payment_flow", "enabled": False},
                rationale="Disable faulty feature flag",
                pre_approved=False,
            )
        ],
        confidence=0.95,
        risk_assessment="Low",
    )

    fake_llm = FakeLLM(response_text=ff_proposal.model_dump_json())
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        convergence_fn=mock_convergence,
        redis_client=AsyncMock(
            get=AsyncMock(
                return_value=json.dumps({
                    "status": "approved",
                    "action_id": "act-ff-1",
                    "proposal": ff_proposal.model_dump(mode="json"),
                })
            )
        ),
    )

    mock_ff_client = MagicMock()
    mock_ff_client.call_tool = AsyncMock(return_value={"status": "flag_toggled"})

    with patch("services.phase2_worker.graph.resolve_mcp_client_for_tool", return_value=mock_ff_client) as mock_resolve:
        initial_state: Phase2GraphState = {
            "incident_id": "inc-ff-1",
            "action_id": "act-ff-1",
            "severity": "P2",
            "merged_finding": {"primary_hypothesis": "Flag regression"},
            "approval_status": "approved",
            "retry_count": 0,
        }

        final_state = await graph.ainvoke(initial_state)

        # Verify resolve_mcp_client_for_tool was called with "toggle_feature_flag"
        mock_resolve.assert_called_once_with("toggle_feature_flag")
        mock_ff_client.call_tool.assert_called_once_with(
            "toggle_feature_flag",
            {
                "flag_name": "new_payment_flow",
                "enabled": False,
                "incident_id": "inc-ff-1",
                "action_id": "act-ff-1",
            },
        )
        assert final_state["outcome"] == "signals_converged"
        assert final_state["execution_results"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_execute_action_missing_action_id_fails_closed(sample_proposal_json):
    fake_llm = FakeLLM(response_text=sample_proposal_json)
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock()

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-no-act-1",
        "severity": "P2",
        "merged_finding": {"primary_hypothesis": "OOMKilled pod"},
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)
    assert final_state["outcome"] == "execution_failed"
    assert "missing incident_id or action_id" in final_state.get("error", "")
    mock_executor.assert_not_called()


@pytest.mark.asyncio
async def test_propose_action_pre_approved_with_real_proposal():
    from unittest.mock import MagicMock, patch
    from shared.models.actions import ActionProposal, ActionPlanItem
    from shared.config.schemas import ToolPolicy
    from services.phase2_worker.graph import create_remediation_graph

    proposal = ActionProposal(
        incident_id="inc-pre-1",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice",
                parameters={"namespace": "boutique", "pod_name": "cartservice-123"},
                rationale="Restart pod",
                pre_approved=True,
            )
        ],
        confidence=0.95,
        risk_assessment="Low",
    )
    fake_llm = FakeLLM(response_text=proposal.model_dump_json())
    mock_qdrant = AsyncMock(return_value={"results": []})
    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
    )
    with patch("shared.config.schemas.SAGPoliciesConfig.get_tool_policy") as mock_policy:
        mock_policy.return_value = ToolPolicy(pre_approved=True, requires_approval=False)
        initial_state: Phase2GraphState = {
            "incident_id": "inc-pre-1",
            "severity": "P2",
            "merged_finding": {"primary_hypothesis": "test"},
            "retry_count": 0,
        }
        final_state = await graph.ainvoke(initial_state)
        assert final_state.get("pre_approved") is True


@pytest.mark.asyncio
async def test_execute_action_injects_server_ids_and_goes_through_sag_guard():
    import json
    from unittest.mock import MagicMock, patch, MagicMock, AsyncMock
    from shared.models.actions import ActionProposal, ActionPlanItem
    from services.phase2_worker.graph import create_remediation_graph

    # Proposal with attacker/stale parameters trying to override incident_id and action_id
    proposal = ActionProposal(
        incident_id="inc-server-1",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="boutique/cartservice",
                parameters={"namespace": "boutique", "pod_name": "cartservice-1", "incident_id": "malicious-id", "action_id": "stale-act"},
                rationale="Restart pod",
            )
        ],
        confidence=0.9,
        risk_assessment="Low",
    )
    fake_llm = FakeLLM(response_text=proposal.model_dump_json())
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_redis = AsyncMock()
    # Mock approved record matching server action_id
    mock_redis.get.return_value = json.dumps({
        "status": "approved",
        "action_id": "act-server-99",
        "proposal": proposal.model_dump(mode="json"),
    })

    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(return_value={"status": "restarted"})

    with patch("services.phase2_worker.graph.resolve_mcp_client_for_tool", return_value=mock_client), \
         patch("shared.harness.sag_guard.SAGGuard.pre_tool_call", new_callable=AsyncMock) as mock_guard_pre:
        graph = create_remediation_graph(
            llm=fake_llm,
            qdrant_fn=mock_qdrant,
            redis_client=mock_redis,
            convergence_fn=AsyncMock(return_value=(True, {})),
        )
        initial_state: Phase2GraphState = {
            "incident_id": "inc-server-1",
            "action_id": "act-server-99",
            "severity": "P2",
            "merged_finding": {"primary_hypothesis": "crash"},
            "approval_status": "approved",
            "retry_count": 0,
        }
        final_state = await graph.ainvoke(initial_state)

        # Assert guard was invoked with server-supplied ids
        mock_guard_pre.assert_called_once()
        guard_call_args = mock_guard_pre.call_args
        assert guard_call_args[0][0] == "restart_pod"
        # server-supplied IDs override any param injection in arguments dict
        assert guard_call_args[0][1]["incident_id"] == "inc-server-1"
        assert guard_call_args[0][1]["action_id"] == "act-server-99"
        # And positional incident_id and action_id match server-supplied
        assert guard_call_args[0][2] == "inc-server-1"
        assert guard_call_args[0][3] == "act-server-99"

        # Assert actual tool execution also received server-supplied IDs
        mock_client.call_tool.assert_any_call(
            "restart_pod",
            {
                "namespace": "boutique",
                "pod_name": "cartservice-1",
                "incident_id": "inc-server-1",
                "action_id": "act-server-99",
            },
        )


@pytest.mark.asyncio
async def test_execute_action_dispatches_execution_success_transition(sample_proposal_json):
    fake_llm = FakeLLM(response_text=sample_proposal_json)
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock(return_value={"status": "ok"})
    mock_transition = AsyncMock(return_value=True)
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
        transition_fn=mock_transition,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-trans-success",
        "action_id": "act-succ-1",
        "severity": "P2",
        "action_proposal": {
            "actions": [
                {
                    "tool_name": "restart_pod",
                    "parameters": {"namespace": "boutique", "pod_name": "cartservice-1"},
                }
            ],
            "confidence": 0.95,
        },
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)
    assert final_state["outcome"] == "signals_converged"

    # Verify execution_success transition was called before convergence finished
    mock_transition.assert_called_once()
    assert mock_transition.call_args[0][0] == "inc-trans-success"
    assert mock_transition.call_args[0][1] == "execution_success"
    assert "execution_results" in mock_transition.call_args[1]["metadata"]


@pytest.mark.asyncio
async def test_execute_action_overrides_hallucinated_namespace_with_incident_namespace():
    mock_executor = AsyncMock(return_value={"status": "ok"})
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-ns-override",
        "action_id": "act-ns-1",
        "affected_namespace": "sock-shop",
        "severity": "P2",
        "action_proposal": {
            "actions": [
                {
                    "tool_name": "update_resource_limits",
                    "parameters": {
                        "namespace": "production",  # LLM hallucinated 'production'
                        "deployment": "catalogue-db",
                        "limits": {"cpu": "1000m", "memory": "1Gi"},
                    },
                }
            ],
            "confidence": 0.95,
        },
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)
    assert final_state["outcome"] == "signals_converged"
    mock_executor.assert_called_once()
    called_params = mock_executor.call_args[0][1]
    # Verify namespace was normalized to 'sock-shop', NOT 'production'
    assert called_params["namespace"] == "sock-shop"
    assert called_params["deployment"] == "catalogue-db"


@pytest.mark.asyncio
async def test_execute_action_catches_fastmcp_inner_tool_error():
    # FastMCP returns 200 envelope with inner text JSON containing isError: true
    fastmcp_error_payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"success": False, "isError": True, "error": "Deployment not found"}),
            }
        ],
        "isError": False,
    }
    mock_executor = AsyncMock(return_value=fastmcp_error_payload)
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-fastmcp-err",
        "action_id": "act-err-1",
        "severity": "P2",
        "action_proposal": {
            "actions": [
                {
                    "tool_name": "restart_pod",
                    "parameters": {"namespace": "sock-shop", "pod_name": "broken-pod"},
                }
            ],
            "confidence": 0.95,
        },
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)
    assert final_state["outcome"] == "execution_failed"
    assert "Deployment not found" in final_state.get("error", "")


# =========================================================================
# Unit Tests for Pre-Execution Gate (Hard-Mode Fault Resilience)
# =========================================================================

def test_classify_live_fault_signature_counter_invariance():
    # 1. Monotonic counter progression (restart_count 3 -> 12) stays in canonical OOM_KILLED
    diag_restarts_3 = {
        "found": True,
        "pod_count": 1,
        "critical_issues": ["Exit Code 137 (OOMKilled)"],
        "pods": [
            {
                "name": "cartservice-abc-1",
                "containers": [
                    {
                        "name": "server",
                        "ready": False,
                        "restart_count": 3,
                        "terminated_reason": "OOMKilled",
                        "exit_code": 137,
                    }
                ],
            }
        ],
    }
    sig3 = classify_live_fault_signature(diag_restarts_3)
    assert sig3["failure_class"] == "OOM_KILLED"

    diag_restarts_12 = {
        "found": True,
        "pod_count": 1,
        "critical_issues": ["Exit Code 137 (OOMKilled)"],
        "pods": [
            {
                "name": "cartservice-abc-1",
                "containers": [
                    {
                        "name": "server",
                        "ready": False,
                        "restart_count": 12,
                        "terminated_reason": "OOMKilled",
                        "exit_code": 137,
                    }
                ],
            }
        ],
    }
    sig12 = classify_live_fault_signature(diag_restarts_12)
    assert sig12["failure_class"] == "OOM_KILLED"

    # 2. Healthy pods (no errors, all ready)
    diag_healthy = {
        "found": True,
        "pod_count": 1,
        "critical_issues": [],
        "pods": [
            {
                "name": "cartservice-abc-1",
                "containers": [
                    {"name": "server", "ready": True, "restart_count": 0}
                ],
            }
        ],
    }
    sig_healthy = classify_live_fault_signature(diag_healthy)
    assert sig_healthy["failure_class"] == "HEALTHY"
    assert sig_healthy["ready_pods"] == 1

    # 3. Image pull failure
    diag_image = {
        "found": True,
        "pod_count": 1,
        "critical_issues": ["Container in ImagePullBackOff"],
        "pods": [
            {
                "name": "frontend-xyz",
                "containers": [
                    {"name": "server", "ready": False, "waiting_reason": "ImagePullBackOff"}
                ],
            }
        ],
    }
    sig_image = classify_live_fault_signature(diag_image)
    assert sig_image["failure_class"] == "IMAGE_PULL_FAILURE"

    # 4. Container configuration error
    diag_config = {
        "found": True,
        "pod_count": 1,
        "critical_issues": [],
        "pods": [
            {
                "name": "frontend-xyz",
                "containers": [
                    {"name": "server", "ready": False, "waiting_reason": "CreateContainerConfigError"}
                ],
            }
        ],
    }
    sig_config = classify_live_fault_signature(diag_config)
    assert sig_config["failure_class"] == "CONFIG_ERROR"


def test_evaluate_pre_execution_gate_verdicts():
    # FAULT_CONFIRMED_PROCEED: update_resource_limits is compatible with OOM_KILLED
    diag_oom = {
        "found": True,
        "pod_count": 1,
        "pods": [{"containers": [{"ready": False, "terminated_reason": "OOMKilled", "exit_code": 137}]}],
    }
    eval_proceed = evaluate_pre_execution_gate(
        tool_name="update_resource_limits",
        tool_params={"deployment": "cartservice"},
        diag=diag_oom,
    )
    assert eval_proceed["verdict"] == "FAULT_CONFIRMED_PROCEED"

    # RESOLVED_SKIP: restart_pod skipped when pods are HEALTHY
    diag_healthy = {
        "found": True,
        "pod_count": 1,
        "pods": [{"containers": [{"ready": True}]}],
    }
    eval_resolved = evaluate_pre_execution_gate(
        tool_name="restart_pod",
        tool_params={"pod_name": "cartservice-123"},
        diag=diag_healthy,
    )
    assert eval_resolved["verdict"] == "RESOLVED_SKIP"

    # MUTATION_DETECTED_REPLAN: scale_deployment contraindicated for IMAGE_PULL_FAILURE
    diag_image = {
        "found": True,
        "pod_count": 1,
        "pods": [{"containers": [{"ready": False, "waiting_reason": "ImagePullBackOff"}]}],
    }
    eval_mutated = evaluate_pre_execution_gate(
        tool_name="scale_deployment",
        tool_params={"deployment": "frontend"},
        diag=diag_image,
    )
    assert eval_mutated["verdict"] == "MUTATION_DETECTED_REPLAN"


@pytest.mark.asyncio
async def test_execute_action_self_resolved_skips_write_and_records_ledger(monkeypatch):
    diag_healthy = {
        "found": True,
        "pod_count": 1,
        "critical_issues": [],
        "recent_events": [],
        "pods": [{"containers": [{"name": "server", "ready": True}]}],
    }
    mock_diag_tool = AsyncMock(return_value=diag_healthy)
    monkeypatch.setattr("services.mcp.tools.kubernetes.k8s_get_pod_diagnostics", mock_diag_tool)

    from unittest.mock import MagicMock
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"state": "EXECUTING", "affected_namespace": "boutique"})

    mock_executor = AsyncMock(return_value={"status": "restarted"})
    mock_convergence = AsyncMock(return_value=(True, {}))

    graph = create_remediation_graph(
        write_executor_fn=mock_executor,
        convergence_fn=mock_convergence,
        redis_client=mock_redis,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-self-res-1",
        "action_id": "act-res-1",
        "severity": "P2",
        "action_proposal": {
            "actions": [
                {
                    "tool_name": "restart_pod",
                    "parameters": {"pod_name": "cartservice-123", "namespace": "boutique"},
                }
            ],
            "confidence": 0.95,
        },
        "approval_status": "approved",
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    # 1. Must skip write tool execution because condition self-resolved
    mock_executor.assert_not_called()
    assert final_state["outcome"] == "signals_converged"

    # 2. Must record diagnostics and self-resolved events in append-only ledger
    rpush_calls = [call[0] for call in mock_redis.rpush.call_args_list]
    keys = [c[0] for c in rpush_calls]
    assert all(k == "incident:inc-self-res-1:ledger" for k in keys)
    payloads = [json.loads(c[1]) for c in rpush_calls]
    event_types = [p["event_type"] for p in payloads]
    assert "pre_execution_diagnostics" in event_types
    assert "pre_execution_self_resolved" in event_types


@pytest.mark.asyncio
async def test_execute_action_mutation_detected_aborts_and_triggers_replan(monkeypatch):
    diag_image_pull = {
        "found": True,
        "pod_count": 1,
        "critical_issues": ["Container waiting on ImagePullBackOff"],
        "recent_events": ["Failed to pull image invalid-tag"],
        "pods": [{"containers": [{"name": "server", "ready": False, "waiting_reason": "ImagePullBackOff"}]}],
    }
    mock_diag_tool = AsyncMock(return_value=diag_image_pull)
    monkeypatch.setattr("services.mcp.tools.kubernetes.k8s_get_pod_diagnostics", mock_diag_tool)

    from unittest.mock import MagicMock
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"state": "EXECUTING", "affected_namespace": "boutique"})
    mock_transition = AsyncMock(return_value=True)
    mock_executor = AsyncMock(return_value={"status": "scaled"})

    # FakeLLM provides reformulated rollback proposal when re-plan triggers
    replan_proposal = ActionProposal(
        incident_id="inc-mut-1",
        actions=[
            ActionPlanItem(
                tool_name="rollback_release",
                target_resource="boutique/frontend",
                parameters={"namespace": "boutique", "deployment": "frontend"},
                rationale="Roll back broken image rollout",
                pre_approved=False,
            )
        ],
        confidence=0.95,
        risk_assessment="Low risk rollback",
    )
    fake_llm = FakeLLM(response_text=replan_proposal.model_dump_json())
    mock_qdrant = AsyncMock(return_value={"results": []})

    graph = create_remediation_graph(
        llm=fake_llm,
        qdrant_fn=mock_qdrant,
        write_executor_fn=mock_executor,
        redis_client=mock_redis,
        transition_fn=mock_transition,
    )

    initial_state: Phase2GraphState = {
        "incident_id": "inc-mut-1",
        "action_id": "act-mut-1",
        "severity": "P2",
        "action_proposal": {
            "actions": [
                {
                    "tool_name": "scale_deployment",
                    "parameters": {"deployment": "frontend", "replicas": 3, "namespace": "boutique"},
                }
            ],
            "confidence": 0.95,
        },
        "approval_status": "approved",
        "retry_count": 0,
        "pre_exec_replan_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    # 1. Incompatible write action must NOT be executed
    mock_executor.assert_not_called()

    # 2. State machine must be triggered with 'pre_exec_replan' transition
    mock_transition.assert_called_once()
    assert mock_transition.call_args[0][0] == "inc-mut-1"
    assert mock_transition.call_args[0][1] == "pre_exec_replan"

    # 3. Graph routed to formulate_fix with mutation_delta and re-planned
    assert final_state.get("pre_exec_replan_count") == 1
    assert "mutation_delta" in final_state
    assert "IMAGE_PULL_FAILURE" in final_state["mutation_delta"]
    assert final_state["action_proposal"]["actions"][0]["tool_name"] == "rollback_release"

    # 4. Ledger records pre_execution_mutation_detected
    rpush_calls = [call[0] for call in mock_redis.rpush.call_args_list]
    payloads = [json.loads(c[1]) for c in rpush_calls]
    event_types = [p["event_type"] for p in payloads]
    assert "pre_execution_mutation_detected" in event_types
    # Verify Redis mutation delta was set
    mock_redis.set.assert_any_call("incident:inc-mut-1:mutation_delta", final_state["mutation_delta"])


@pytest.mark.asyncio
async def test_formulate_fix_includes_agent_findings_and_phase1_evidence():
    captured_prompts = []

    class CapturingFakeLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            captured_prompts.append("\n".join(str(getattr(m, "content", m)) for m in messages))
            return await super().ainvoke(messages, **kwargs)

    proposal = ActionProposal(
        incident_id="inc-ff-1",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable active cartFailure flag reported by change_config_investigator",
                pre_approved=False,
            )
        ],
        confidence=0.95,
        risk_assessment="Low risk flag toggle",
    )
    llm = CapturingFakeLLM(response_text=proposal.model_dump_json())
    mock_qdrant = AsyncMock(return_value={"results": []})
    graph = create_remediation_graph(llm=llm, qdrant_fn=mock_qdrant)

    initial_state: Phase2GraphState = {
        "incident_id": "inc-ff-1",
        "severity": "P2",
        "affected_service": "frontend",
        "affected_namespace": "otel-demo",
        "merged_finding": {
            "primary_hypothesis": "Frontend gateway timeout on cart requests",
            "verbatim_signals": {},
            "findings_by_agent": {
                "change_config_investigator": {
                    "confidence": 0.9,
                    "hypothesis": "Active feature flag cartFailure has defaultVariant 100%.",
                    "recommended_actions": ["Toggle cartFailure off"],
                }
            },
            "all_evidence": [
                {
                    "source_tool": "get_feature_flag",
                    "query_or_cmd": "get_feature_flag()",
                    "raw_output": '{"active_flags": {"cartFailure": {"defaultVariant": "100%"}}}',
                }
            ],
        },
        "retry_count": 0,
    }

    final_state = await graph.ainvoke(initial_state)

    assert final_state["outcome"] == "action_proposed"
    assert len(captured_prompts) == 1
    assert "<investigator_findings>" in captured_prompts[0]
    assert "<phase1_evidence>" in captured_prompts[0]
    assert "cartFailure" in captured_prompts[0]
    assert "get_feature_flag" in captured_prompts[0]


@pytest.mark.asyncio
async def test_full_order_merge_to_phase2_real_convergence():
    """Exercise full pipeline order: Phase 1 merge_findings -> Phase 2 formulate_fix -> real convergence."""
    from unittest.mock import MagicMock, patch
    from services.phase1_worker.merge import merge_findings
    from shared.models.enums import AgentRole
    from shared.models.findings import AgentFinding, EvidenceItem

    captured_prompts = []

    class CapturingFakeLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            captured_prompts.append("\n".join(str(getattr(m, "content", m)) for m in messages))
            return await super().ainvoke(messages, **kwargs)

    # 1. Real Phase 1 findings (including a 0-tool finding that must NOT be dropped)
    f_cfg = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="cartFailure enabled at on",
        confidence=0.92,
        evidence=[
            EvidenceItem(
                source_tool="get_feature_flag",
                query_or_cmd="get_feature_flag()",
                raw_output='{"cartFailure": {"defaultVariant": "on"}}',
            )
        ],
        verbatim_signals={"flag_name": "cartFailure"},
        recommended_actions=["toggle_feature_flag cartFailure off"],
    )
    f_infra_zero = AgentFinding(
        agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
        hypothesis="[no tools called, 0 evidence] No pod check done",
        confidence=0.40,
        evidence=[],
        verbatim_signals={"tool_execution_status": "no tools called, 0 evidence"},
    )
    merged = merge_findings([f_infra_zero, f_cfg], incident_id="inc-full-order")

    proposal = ActionProposal(
        incident_id="inc-full-order",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable cartFailure",
                pre_approved=True,
            )
        ],
        confidence=0.92,
        risk_assessment="Low risk toggle",
    )
    llm = CapturingFakeLLM(response_text=proposal.model_dump_json())
    mock_qdrant = AsyncMock(return_value={"results": []})
    mock_executor = AsyncMock(return_value={"status": "updated"})

    # Real Redis contract for <recent_recurrence_window> + real run_convergence_monitor backing
    redis_data = {
        "incident:inc-full-order:alert_group": json.dumps(
            {
                "chronic_recurrence": True,
                "recurrence_count": 3,
                "recent_recurrences": [
                    {
                        "incident_id": "inc-prev-1",
                        "alertname": "CartDown",
                        "outcome": "ESCALATED",
                        "hypothesis": "Pod hung on missing DB conn " + ("x" * 300),
                        "last_action_tool": "restart_pod",
                        "action_rationale": "Attempted restart to reset pooled connections " + ("y" * 300),
                    }
                ],
            }
        )
    }
    mock_redis = MagicMock()
    mock_redis.get.side_effect = lambda k: redis_data.get(k)
    mock_redis.set.side_effect = lambda k, v, *a, **kw: redis_data.__setitem__(k, v)
    mock_redis.rpush.side_effect = lambda k, v: None

    class StubPromMCP:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, name, arguments):
            self.calls += 1
            return {"content": [{"type": "text", "text": "0.0"}]}

    class StubK8sMCP:
        async def call_tool(self, name, arguments):
            return {"pods": [{"name": "cart-0", "phase": "Running", "ready": True, "container_statuses": []}]}

    mock_prom_mcp = StubPromMCP()
    mock_k8s_mcp = StubK8sMCP()

    async def fast_sleep(_seconds):
        return None

    with patch("services.phase2_worker.convergence.get_mcp_client", return_value=mock_prom_mcp), \
         patch("services.phase2_worker.convergence.asyncio.sleep", side_effect=fast_sleep), \
         patch("shared.harness.mcp.resolve_mcp_client_for_tool", return_value=mock_k8s_mcp):
        graph = create_remediation_graph(
            llm=llm,
            qdrant_fn=mock_qdrant,
            write_executor_fn=mock_executor,
            redis_client=mock_redis,
        )
        pass1_state = await graph.ainvoke(
            {
                "incident_id": "inc-full-order",
                "action_id": "act-full-order",
                "severity": "P2",
                "affected_service": "cart",
                "affected_namespace": "otel-demo",
                "merged_finding": merged.model_dump(mode="json"),
                "retry_count": 0,
            }
        )
        assert pass1_state["outcome"] == "action_proposed"
        final_state = await graph.ainvoke(
            {
                "incident_id": "inc-full-order",
                "action_id": pass1_state.get("action_id") or "act-full-order",
                "action_proposal": pass1_state["action_proposal"],
                "approval_status": "approved",
                "affected_service": "cart",
                "affected_namespace": "otel-demo",
                "severity": "P2",
                "retry_count": 0,
            }
        )

    assert len(captured_prompts) == 1
    assert "no tools called, 0 evidence" in captured_prompts[0]
    assert "Ranked Candidate Hypotheses" in captured_prompts[0]
    assert "<recent_recurrence_window>" in captured_prompts[0]
    assert "inc-prev-1" in captured_prompts[0]
    assert "restart_pod" in captured_prompts[0]
    # Full hypothesis and action_rationale must NOT be truncated mid-sentence
    assert ("Pod hung on missing DB conn " + ("x" * 300)) in captured_prompts[0]
    assert ("Attempted restart to reset pooled connections " + ("y" * 300)) in captured_prompts[0]
    assert final_state["outcome"] == "signals_converged"
    assert mock_prom_mcp.calls > 0


@pytest.mark.asyncio
async def test_phase2_caps_prompt_3_per_agent_5_total_without_dropping_state():
    """Phase 2 prompt receives max 3 per agent and max 5 total findings/evidence without dropping MergedFinding."""
    from services.phase1_worker.merge import merge_findings
    from shared.models.enums import AgentRole
    from shared.models.findings import AgentFinding, EvidenceItem

    captured = []

    class CapturingFakeLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            captured.append("\n".join(str(getattr(m, "content", m)) for m in messages))
            return await super().ainvoke(messages, **kwargs)

    # 1. Single agent with 6 findings/evidence items + 2 other specialists (low confidence)
    infra_many = [
        AgentFinding(
            agent_role=AgentRole.INFRASTRUCTURE_INVESTIGATOR,
            hypothesis=f"Infra subfinding #{i}",
            confidence=0.95 - i * 0.02,
            evidence=[
                EvidenceItem(source_tool=f"k8s_tool_{j}", query_or_cmd=f"q-{i}-{j}", raw_output=f"out-{i}-{j}")
                for j in range(5)
            ],
        )
        for i in range(4)
    ]
    log_finding = AgentFinding(
        agent_role=AgentRole.LOG_TRACE_INVESTIGATOR,
        hypothesis="Tempo timeout trace",
        confidence=0.55,
        evidence=[EvidenceItem(source_tool="tempo_search", query_or_cmd="trace-1", raw_output="timeout")],
    )
    cfg_low = AgentFinding(
        agent_role=AgentRole.CHANGE_CONFIG_INVESTIGATOR,
        hypothesis="Flag cartFailure active",
        confidence=0.15,
        evidence=[EvidenceItem(source_tool="get_feature_flag", query_or_cmd="flagd()", raw_output="on")],
    )
    merged = merge_findings(infra_many + [log_finding, cfg_low], incident_id="inc-p2-cap")
    # MergedFinding itself retains all 6 findings
    assert len(merged.ranked_findings) == 6

    proposal = ActionProposal(
        incident_id="inc-p2-cap",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable flag",
                pre_approved=False,
            )
        ],
        confidence=0.9,
        risk_assessment="Low",
    )
    graph = create_remediation_graph(
        llm=CapturingFakeLLM(response_text=proposal.model_dump_json()),
        qdrant_fn=AsyncMock(return_value={"results": []}),
    )
    out_state = await graph.ainvoke(
        {
            "incident_id": "inc-p2-cap",
            "severity": "P2",
            "affected_service": "cart",
            "affected_namespace": "otel-demo",
            "merged_finding": merged.model_dump(mode="json"),
            "retry_count": 0,
        }
    )

    assert len(captured) == 1
    inv_block = captured[0].split("<investigator_findings>")[-1].split("</investigator_findings>")[0]
    ev_block = captured[0].split("<phase1_evidence>")[-1].split("</phase1_evidence>")[0]
    inv_json_str = inv_block[inv_block.index("["):]
    inv_items = json.loads(inv_json_str)
    ev_items = json.loads(ev_block.strip())

    # Total <= 5, per agent <= 3, low-confidence ChangeConfig guaranteed present
    assert len(inv_items) == 5
    infra_count = sum(1 for x in inv_items if x["agent"] == "infrastructure_investigator")
    assert infra_count <= 3
    assert any(x["agent"] == "change_config_investigator" for x in inv_items)
    assert all(len(x["evidence"]) <= 3 for x in inv_items)
    assert len(ev_items) == 5
    assert any(e["source_tool"] == "get_feature_flag" for e in ev_items)
    # State retains original undropped merged_finding
    assert len(out_state["merged_finding"]["ranked_findings"]) == 6


@pytest.mark.asyncio
async def test_execute_action_uses_merged_hypothesis_for_hung_process_and_multi_service_convergence():
    """Verify execute_action reads merged_finding primary_hypothesis (no RESOLVED_SKIP on hung pods) and convergence monitors both primary & target services."""
    from unittest.mock import MagicMock, patch

    proposal = ActionProposal(
        incident_id="inc-hung-1",
        actions=[
            ActionPlanItem(
                tool_name="restart_pod",
                target_resource="otel-demo/checkout",
                parameters={"namespace": "otel-demo", "pod_name": "checkout-7d8f9c6b5-k2m9z"},
                rationale="Restart deadlocked checkout pod",
                pre_approved=True,
            )
        ],
        confidence=0.92,
        risk_assessment="Low risk pod restart",
    )

    mock_k8s_diag = MagicMock()
    mock_k8s_diag.call_tool = AsyncMock(
        return_value={
            "found": True,
            "pods": [
                {
                    "name": "checkout-7d8f9c6b5-k2m9z",
                    "phase": "Running",
                    "ready": True,
                    "restart_count": 0,
                    "container_statuses": [
                        {"name": "checkout", "ready": True, "state": {"running": {"startedAt": "2026-11-22T00:00:00Z"}}}
                    ],
                }
            ],
        }
    )
    mock_executor = AsyncMock(return_value={"success": True, "restarted": "checkout-7d8f9c6b5-k2m9z"})
    captured_conv_services = []

    async def capture_conv(incident_id, affected_services, severity):
        captured_conv_services.extend(affected_services)
        return True, {}

    with patch("services.phase2_worker.graph.resolve_mcp_client_for_tool", return_value=mock_k8s_diag):
        graph = create_remediation_graph(
            llm=FakeLLM(response_text=proposal.model_dump_json()),
            qdrant_fn=AsyncMock(return_value={"results": []}),
            write_executor_fn=mock_executor,
            convergence_fn=capture_conv,
        )
        final_state = await graph.ainvoke(
            {
                "incident_id": "inc-hung-1",
                "action_id": "act-hung-1",
                "approval_status": "approved",
                "action_proposal": proposal.model_dump(mode="json"),
                "severity": "P2",
                "affected_service": "frontend",
                "affected_namespace": "otel-demo",
                "merged_finding": {"primary_hypothesis": "Checkout service process deadlocked and hung"},
                "retry_count": 0,
            }
        )

    # Must NOT have skipped restart_pod via RESOLVED_SKIP
    mock_executor.assert_awaited_once()
    assert final_state["outcome"] == "signals_converged"
    # Convergence must poll BOTH affected_service (frontend) and remediated target (checkout)
    assert "frontend" in captured_conv_services
    assert "checkout" in captured_conv_services


@pytest.mark.asyncio
async def test_formulate_fix_attaches_runbook_citations_and_text_refs():
    """Verify formulate_fix_node enriches ActionProposal with structured RunbookCitation metadata and inline text refs."""
    from unittest.mock import MagicMock
    from langchain_core.messages import AIMessage

    tool_call_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "qdrant_search",
                "args": {"id": "feature-flag-failure"},
                "id": "call-rb-1",
            }
        ],
    )
    proposal_without_cits = ActionProposal(
        incident_id="inc-cit-p2",
        actions=[
            ActionPlanItem(
                tool_name="toggle_feature_flag",
                target_resource="otel-demo/cartFailure",
                parameters={"flag_name": "cartFailure", "enabled": False, "environment": "otel-demo"},
                rationale="Disable active cartFailure flag",
            )
        ],
        confidence=0.95,
        risk_assessment="Low risk toggle",
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[tool_call_msg, proposal_without_cits])

    async def fake_qdrant(**kwargs):
        return {
            "results": [
                {
                    "id": "pt-ff-4",
                    "score": 0.96,
                    "runbook_id": "feature-flag-failure",
                    "title": "Application Feature Flag Misconfiguration and Failure Toggle Mitigation",
                    "section": "Remediation Steps",
                    "line_start": 46,
                    "line_end": 48,
                    "line_range": "L46-L48",
                    "chunk_index": 4,
                    "total_chunks": 5,
                    "chunk_id": "chunk 4/5",
                    "content": "Call toggle_feature_flag with the exact active flag_name and enabled: false in otel-demo.",
                    "allowed_actions": ["toggle_feature_flag", "restart_pod"],
                }
            ]
        }

    graph = create_remediation_graph(
        llm=mock_llm,
        qdrant_fn=fake_qdrant,
        write_executor_fn=AsyncMock(return_value={"success": True}),
        convergence_fn=AsyncMock(return_value=(True, {})),
    )
    out = await graph.ainvoke(
        {
            "incident_id": "inc-cit-p2",
            "severity": "P2",
            "affected_service": "cart",
            "affected_namespace": "otel-demo",
            "merged_finding": {"primary_hypothesis": "cartFailure feature flag enabled"},
            "retry_count": 0,
        }
    )

    prop = out["action_proposal"]
    assert len(prop["runbook_citations"]) == 1
    cit = prop["runbook_citations"][0]
    assert cit["runbook_id"] == "feature-flag-failure"
    assert cit["book_name"] == "Application Feature Flag Misconfiguration and Failure Toggle Mitigation"
    assert cit["section"] == "Remediation Steps"
    assert cit["line_range"] == "L46-L48"
    assert cit["chunk_id"] == "chunk 4/5"
    assert "toggle_feature_flag" in cit["referenced_text"]

    # Structured multi-line reference must appear on a new line in action and top-level rationale
    act_rat = prop["actions"][0]["rationale"]
    assert act_rat.startswith("Disable active cartFailure flag\n\n[Runbook Reference]\n")
    assert "id/title: 'feature-flag-failure/Application Feature Flag Misconfiguration and Failure Toggle Mitigation'" in act_rat
    assert "section/line_range: 'Remediation Steps/L46-L48 (chunk 4/5)'" in act_rat
    assert 'reference: "' in act_rat
    assert "\n\n[Runbook Reference]\n" in prop["rationale"]







