import pytest
import httpx
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.tempo import (
    tempo_search,
    tempo_get_trace,
    validate_service_name,
    validate_trace_id,
    validate_duration_ms,
    register_tools,
)


@pytest.mark.asyncio
async def test_tempo_search_success(mocker):
    mock_response = {
        "traces": [
            {
                "traceID": "4b6f1234567890ab",
                "rootServiceName": "cartservice",
                "rootTraceName": "AddItem",
                "startTimeUnixNano": "1726910000000000000",
                "durationMs": 120,
            }
        ]
    }

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_response
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await tempo_search(
        service="cartservice",
        error=True,
        min_duration_ms=50,
        time_range="5m",
        client=mock_client,
        base_url="http://mock-tempo:3200",
    )

    assert res["service"] == "cartservice"
    assert res["error_filtered"] is True
    assert res["trace_count"] == 1
    assert res["traces"][0]["trace_id"] == "4b6f1234567890ab"
    assert res["traces"][0]["duration_ms"] == 120


@pytest.mark.asyncio
async def test_tempo_search_invalid_inputs():
    with pytest.raises(ValueError, match="Invalid service name"):
        await tempo_search(service="cartservice; rm -rf")

    with pytest.raises(ValueError, match="min_duration_ms must be between"):
        await tempo_search(service="cartservice", min_duration_ms=-1)

    with pytest.raises(ValueError, match="Invalid time_range"):
        await tempo_search(service="cartservice", time_range="not_valid")


@pytest.mark.asyncio
async def test_tempo_get_trace_success(mocker):
    mock_response = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "cartservice"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "4b6f1234567890ab4b6f1234567890ab",
                                "spanId": "span12345",
                                "name": "AddItem",
                                "startTimeUnixNano": "1726910000000000000",
                                "endTimeUnixNano": "1726910000120000000",
                                "status": {"code": 1},
                            }
                        ]
                    }
                ],
            }
        ]
    }

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_response
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await tempo_get_trace(
        trace_id="4b6f1234567890ab4b6f1234567890ab",
        client=mock_client,
        base_url="http://mock-tempo:3200",
    )

    assert res["trace_id"] == "4b6f1234567890ab4b6f1234567890ab"
    assert res["found"] is True
    assert res["span_count"] == 1
    assert res["spans"][0]["service_name"] == "cartservice"
    assert res["spans"][0]["name"] == "AddItem"


@pytest.mark.asyncio
async def test_tempo_get_trace_invalid_hex():
    with pytest.raises(ValueError, match="Invalid trace_id"):
        await tempo_get_trace("invalid_trace_xyz")

    with pytest.raises(ValueError, match="Invalid trace_id"):
        await tempo_get_trace("1234")  # too short (< 16 hex chars)


@pytest.mark.asyncio
async def test_tempo_get_trace_not_found(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 404
    mock_client.get.return_value = mock_resp_obj

    res = await tempo_get_trace(
        trace_id="4b6f1234567890ab",
        client=mock_client,
        base_url="http://mock-tempo:3200",
    )

    assert res["found"] is False
    assert res["span_count"] == 0
    assert "not found" in res["message"]


def test_tempo_tool_registration():
    app = FastMCP("test-server")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "tempo_search" in tools
    assert "tempo_get_trace" in tools
