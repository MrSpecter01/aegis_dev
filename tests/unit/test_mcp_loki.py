import pytest
import httpx
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.loki import (
    loki_query,
    validate_logql_query,
    validate_limit,
    register_tools,
)


@pytest.mark.asyncio
async def test_loki_query_success(mocker):
    mock_response = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"app": "cartservice", "namespace": "boutique"},
                    "values": [
                        ["1726910001000000000", "error: OutOfMemoryException"],
                        ["1726910002000000000", "terminating connection to redis"],
                    ],
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

    res = await loki_query(
        logql='{app="cartservice"} |= "error"',
        time_range="5m",
        limit=50,
        client=mock_client,
        base_url="http://mock-loki:3100",
    )

    assert res["query"] == '{app="cartservice"} |= "error"'
    assert res["time_range"] == "5m"
    assert res["limit"] == 50
    assert res["line_count"] == 2
    assert "error: OutOfMemoryException" in res["lines"]
    assert res["stream_labels"]["app"] == "cartservice"


@pytest.mark.asyncio
async def test_loki_limit_cap_at_500(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = {"status": "success", "data": {"result": []}}
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await loki_query(
        logql='{app="cartservice"}',
        time_range="10m",
        limit=1000,  # exceeds 500
        client=mock_client,
        base_url="http://mock-loki:3100",
    )

    assert res["limit"] == 500
    # Check that HTTP params sent limit=500
    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["limit"] == 500


@pytest.mark.asyncio
async def test_loki_limit_below_one():
    with pytest.raises(ValueError, match="Limit must be at least 1"):
        await loki_query(logql='{app="cartservice"}', limit=0)


@pytest.mark.asyncio
async def test_loki_missing_stream_selector():
    with pytest.raises(ValueError, match="must start with a stream selector"):
        await loki_query(logql='app="cartservice"')


@pytest.mark.asyncio
async def test_loki_disallowed_injection_chars():
    with pytest.raises(ValueError, match="disallowed control or injection characters"):
        await loki_query(logql='{app="cartservice"}; rm -rf')

    with pytest.raises(ValueError, match="disallowed control or injection characters"):
        await loki_query(logql='{app="cartservice"} `id`')


@pytest.mark.asyncio
async def test_loki_upstream_error(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 502
    mock_resp_obj.text = "Bad Gateway"
    mock_client.get.side_effect = httpx.HTTPStatusError(
        "Gateway Error", request=mocker.MagicMock(), response=mock_resp_obj
    )

    with pytest.raises(RuntimeError, match="Loki HTTP error 502"):
        await loki_query(
            logql='{app="cartservice"}',
            time_range="5m",
            client=mock_client,
            base_url="http://mock-loki:3100",
        )


def test_loki_tool_registration():
    app = FastMCP("test-server")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "loki_query" in tools
