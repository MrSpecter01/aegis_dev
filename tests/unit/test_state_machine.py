import pytest
from unittest.mock import patch, MagicMock
from shared.models.enums import IncidentState
from services.api_server.state_machine import IncidentMachine

@pytest.fixture
def mock_redis(mocker):
    client = mocker.Mock()
    import redis
    client.__class__ = redis.Redis
    return client

@pytest.fixture
def machine(mock_redis):
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "NEW"}):
        return IncidentMachine("inc-123", mock_redis, "NEW")

@pytest.mark.asyncio
async def test_new_to_grouping_on_alert_received(machine, mock_redis):
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "NEW"}):
        res = await machine.trigger("alert_received", "actor1")
        assert res is True
        assert machine.state == "GROUPING"
        mock_redis.xadd.assert_not_called()

@pytest.mark.asyncio
async def test_grouping_to_triage_on_grouping_complete(machine, mock_redis):
    machine.machine.set_state("GROUPING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "GROUPING"}):
        res = await machine.trigger("grouping_complete", "actor1")
        assert res is True
        assert machine.state == "TRIAGE"
        mock_redis.xadd.assert_called_with("stream:phase1:jobs", {"incident_id": "inc-123"})

@pytest.mark.asyncio
async def test_triage_to_merging_on_triage_complete(machine, mock_redis):
    machine.machine.set_state("TRIAGE")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "TRIAGE"}):
        res = await machine.trigger("triage_complete", "actor1")
        assert res is True
        assert machine.state == "MERGING"

@pytest.mark.asyncio
async def test_merging_to_remediation_on_merge_complete(machine, mock_redis):
    machine.machine.set_state("MERGING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MERGING"}):
        res = await machine.trigger("merge_complete", "actor1")
        assert res is True
        assert machine.state == "REMEDIATION"
        mock_redis.xadd.assert_called_with("stream:phase2:jobs", {"incident_id": "inc-123"})

@pytest.mark.asyncio
async def test_remediation_to_awaiting_approval(machine, mock_redis):
    machine.machine.set_state("REMEDIATION")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "REMEDIATION"}):
        res = await machine.trigger("action_proposed", "actor1")
        assert res is True
        assert machine.state == "AWAITING_APPROVAL"

@pytest.mark.asyncio
async def test_approved_to_executing(machine, mock_redis):
    machine.machine.set_state("AWAITING_APPROVAL")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "AWAITING_APPROVAL"}):
        res = await machine.trigger("approved", "actor1")
        assert res is True
        assert machine.state == "EXECUTING"

@pytest.mark.asyncio
async def test_denied_to_escalated(machine, mock_redis):
    machine.machine.set_state("AWAITING_APPROVAL")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "AWAITING_APPROVAL"}):
        res = await machine.trigger("denied", "actor1")
        assert res is True
        assert machine.state == "ESCALATED"

@pytest.mark.asyncio
async def test_execution_success_to_monitoring(machine, mock_redis):
    machine.machine.set_state("EXECUTING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "EXECUTING"}):
        res = await machine.trigger("execution_success", "actor1")
        assert res is True
        assert machine.state == "MONITORING"

@pytest.mark.asyncio
async def test_execution_failed_to_escalated(machine, mock_redis):
    machine.machine.set_state("EXECUTING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "EXECUTING"}):
        res = await machine.trigger("execution_failed", "actor1")
        assert res is True
        assert machine.state == "ESCALATED"

@pytest.mark.asyncio
async def test_signals_converged_to_reporting(machine, mock_redis):
    machine.machine.set_state("MONITORING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MONITORING"}):
        res = await machine.trigger("signals_converged", "actor1")
        assert res is True
        assert machine.state == "REPORTING"
        mock_redis.xadd.assert_called_with("stream:phase3:jobs", {"incident_id": "inc-123"})

@pytest.mark.asyncio
async def test_signals_not_converged_loops_back(machine, mock_redis):
    machine.machine.set_state("MONITORING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MONITORING"}):
        res = await machine.trigger("signals_not_converged", "actor1")
        assert res is True
        assert machine.state == "REMEDIATION"

@pytest.mark.asyncio
async def test_retry_limit_reached_escalates(machine, mock_redis):
    machine.machine.set_state("MONITORING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MONITORING"}):
        res = await machine.trigger("retry_limit_reached", "actor1")
        assert res is True
        assert machine.state == "ESCALATED"

@pytest.mark.asyncio
async def test_report_delivered_to_closed(machine, mock_redis):
    machine.machine.set_state("REPORTING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "REPORTING"}):
        res = await machine.trigger("report_delivered", "actor1")
        assert res is True
        assert machine.state == "CLOSED"

@pytest.mark.asyncio
async def test_escalate_from_any_active_state(machine, mock_redis):
    machine.machine.set_state("TRIAGE")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "TRIAGE"}):
        res = await machine.trigger("escalate", "actor1")
        assert res is True
        assert machine.state == "ESCALATED"

@pytest.mark.asyncio
async def test_terminal_state_refuses_transition(machine, mock_redis):
    machine.machine.set_state("CLOSED")
    with pytest.raises(ValueError, match="Cannot transition from terminal state"):
        await machine.trigger("escalate", "actor1")

@pytest.mark.asyncio
async def test_terminal_state_refuses_transition_escalated(machine, mock_redis):
    machine.machine.set_state("ESCALATED")
    with pytest.raises(ValueError, match="Cannot transition from terminal state"):
        await machine.trigger("escalate", "actor1")

@pytest.mark.asyncio
async def test_invalid_trigger_from_state_raises_error(machine, mock_redis):
    machine.machine.set_state("NEW")
    with pytest.raises(ValueError, match="Invalid event triage_complete for state NEW"):
        await machine.trigger("triage_complete", "actor1")

@pytest.mark.asyncio
async def test_ledger_entry_written_on_transition(machine, mock_redis):
    with patch("services.api_server.state_machine.append_ledger_entry") as mock_append:
        with patch("services.api_server.incident_store.get_incident", return_value={"state": "NEW"}):
            await machine.trigger("alert_received", "actor1")
            mock_append.assert_called_once()
            entry = mock_append.call_args[0][2]
            assert entry.event_type == "alert_received"
            assert entry.state == IncidentState.GROUPING

@pytest.mark.asyncio
async def test_redis_state_updated_on_transition(machine, mock_redis):
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "NEW"}):
        await machine.trigger("alert_received", "actor1")
        mock_redis.set.assert_called_once()
        args = mock_redis.set.call_args[0]
        import json
        assert args[0] == "incident:inc-123:state"
        assert json.loads(args[1])["state"] == "GROUPING"

def test_load_machine_hydrates_from_redis(mock_redis):
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MERGING"}):
        from services.api_server.state_machine import load_machine
        mach = load_machine("inc-123", mock_redis)
        assert mach.state == "MERGING"

@pytest.mark.asyncio
async def test_phase1_stream_dispatched_on_triage_entry(machine, mock_redis):
    machine.machine.set_state("GROUPING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "GROUPING"}):
        await machine.trigger("grouping_complete", "actor1")
        mock_redis.xadd.assert_called_with("stream:phase1:jobs", {"incident_id": "inc-123"})

@pytest.mark.asyncio
async def test_phase2_stream_dispatched_on_remediation_entry(machine, mock_redis):
    machine.machine.set_state("MERGING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MERGING"}):
        await machine.trigger("merge_complete", "actor1")
        mock_redis.xadd.assert_called_with("stream:phase2:jobs", {"incident_id": "inc-123"})

@pytest.mark.asyncio
async def test_phase3_stream_dispatched_on_reporting_entry(machine, mock_redis):
    machine.machine.set_state("MONITORING")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "MONITORING"}):
        await machine.trigger("signals_converged", "actor1")
        mock_redis.xadd.assert_called_with("stream:phase3:jobs", {"incident_id": "inc-123"})


@pytest.mark.asyncio
async def test_drop_from_active_state_transitions_to_closed(machine, mock_redis):
    machine.machine.set_state("REMEDIATION")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "REMEDIATION"}):
        res = await machine.trigger("drop", "sre_operator", {"reason": "False positive"})
        assert res is True
        assert machine.state == "CLOSED"
        mock_redis.set.assert_any_call("incident:inc-123:dropped", "1")


@pytest.mark.asyncio
async def test_approved_from_escalated_transitions_to_executing(machine, mock_redis):
    machine.machine.set_state("ESCALATED")
    with patch("services.api_server.incident_store.get_incident", return_value={"state": "ESCALATED"}):
        res = await machine.trigger("approved", "sre_operator", {"reason": "Authorized after escalation"})
        assert res is True
        assert machine.state == "EXECUTING"
