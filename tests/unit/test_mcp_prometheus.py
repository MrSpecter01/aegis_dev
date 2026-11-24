import pytest
import httpx
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.prometheus import (
    prometheus_query,
    validate_promql_query,
    validate_time_range,
    register_tools,
)


@pytest.mark.asyncio
async def test_prometheus_query_success(mocker):
    mock_response = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": "up", "instance": "cartservice:8080", "job": "cartservice"},
                    "value": [1726910000, "1"],
                }
            ],
        },
    }

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_response
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await prometheus_query(
        query='up{job="cartservice"}',
        time_range="5m",
        client=mock_client,
        base_url="http://mock-prom:9090",
    )

    assert res["query"] == 'up{job="cartservice"}'
    assert res["time_range"] == "5m"
    assert res["result_count"] == 1
    assert res["results"][0]["metric"]["job"] == "cartservice"
    assert res["results"][0]["values"] == [[1726910000, "1"]]


@pytest.mark.asyncio
async def test_prometheus_query_disallowed_characters():
    with pytest.raises(ValueError, match="disallowed characters"):
        await prometheus_query(query="up; rm -rf /", time_range="5m")

    with pytest.raises(ValueError, match="disallowed characters"):
        await prometheus_query(query="up `whoami`", time_range="5m")

    with pytest.raises(ValueError, match="disallowed characters"):
        await prometheus_query(query="up $USER", time_range="5m")


@pytest.mark.asyncio
async def test_prometheus_query_empty():
    with pytest.raises(ValueError, match="cannot be empty"):
        await prometheus_query(query="", time_range="5m")

    with pytest.raises(ValueError, match="cannot be empty"):
        await prometheus_query(query="up", time_range="")


@pytest.mark.asyncio
async def test_prometheus_invalid_time_range():
    with pytest.raises(ValueError, match="Invalid time_range"):
        await prometheus_query(query="up", time_range="invalid_range")

    with pytest.raises(ValueError, match="Invalid time_range"):
        await prometheus_query(query="up", time_range="-5m")


@pytest.mark.asyncio
async def test_prometheus_upstream_error(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 500
    mock_resp_obj.text = "Internal Server Error"
    mock_client.get.side_effect = httpx.HTTPStatusError(
        "Server Error", request=mocker.MagicMock(), response=mock_resp_obj
    )

    with pytest.raises(RuntimeError, match="Prometheus HTTP error 500"):
        await prometheus_query(
            query="up",
            time_range="5m",
            client=mock_client,
            base_url="http://mock-prom:9090",
        )


def test_prometheus_tool_registration():
    app = FastMCP("test-server")
    register_tools(app)
    # Verify tool is registered in app
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "prometheus_query" in tools
