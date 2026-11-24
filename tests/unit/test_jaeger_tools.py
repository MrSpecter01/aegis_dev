import pytest
import httpx
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.jaeger import (
    validate_service_name,
    validate_trace_id,
    validate_duration_ms,
    jaeger_search_traces,
    jaeger_get_trace,
    register_tools,
)


def test_validate_service_name():
    validate_service_name("ad-service")
    validate_service_name("frontend.demo")
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_service_name("")
    with pytest.raises(ValueError, match="Invalid service name"):
        validate_service_name("service;rm -rf")


def test_validate_trace_id():
    validate_trace_id("30ed029d7d0b2478a936e64657f9a999")
    validate_trace_id("0123456789abcdef")
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_trace_id("")
    with pytest.raises(ValueError, match="Invalid trace_id"):
        validate_trace_id("not-a-hex-id!")


def test_validate_duration_ms():
    validate_duration_ms(0)
    validate_duration_ms(500)
    with pytest.raises(ValueError, match="between 0 and 600000"):
        validate_duration_ms(-10)
    with pytest.raises(ValueError, match="between 0 and 600000"):
        validate_duration_ms(700000)


@pytest.mark.asyncio
async def test_jaeger_search_traces_success():
    sample_jaeger_response = {
        "data": [
            {
                "traceID": "30ed029d7d0b2478a936e64657f9a999",
                "spans": [
                    {
                        "traceID": "30ed029d7d0b2478a936e64657f9a999",
                        "spanID": "0be3b9df0b449401",
                        "operationName": "GET /metrics",
                        "references": [],
                        "startTime": 1794567196283312,
                        "duration": 412000,
                        "tags": [
                            {"key": "http.response.status_code", "type": "int64", "value": 500},
                            {"key": "error", "type": "bool", "value": True},
                        ],
                        "processID": "p1",
                    }
                ],
                "processes": {
                    "p1": {"serviceName": "ad"}
                },
            }
        ],
        "total": 1,
    }

    async def mock_handler(request: httpx.Request):
        assert "service=ad" in str(request.url)
        return httpx.Response(200, json=sample_jaeger_response)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await jaeger_search_traces(
            service="ad",
            operation="GET /metrics",
            tags="error=true",
            min_duration_ms=100,
            limit=10,
            client=client,
            base_url="http://mock-jaeger:16686",
        )

        assert result["service"] == "ad"
        assert result["total_matches"] == 1
        trace = result["traces"][0]
        assert trace["trace_id"] == "30ed029d7d0b2478a936e64657f9a999"
        assert trace["root_service"] == "ad"
        assert trace["has_error"] is True
        assert trace["duration_ms"] == 412.0


@pytest.mark.asyncio
async def test_jaeger_get_trace_success():
    sample_trace = {
        "data": [
            {
                "traceID": "30ed029d7d0b2478a936e64657f9a999",
                "spans": [
                    {
                        "traceID": "30ed029d7d0b2478a936e64657f9a999",
                        "spanID": "0be3b9df0b449401",
                        "operationName": "GET /ads",
                        "startTime": 1000000,
                        "duration": 50000,
                        "tags": [{"key": "error", "value": True}],
                        "processID": "p1",
                    }
                ],
                "processes": {
                    "p1": {"serviceName": "ad"}
                },
            }
        ]
    }

    async def mock_handler(request: httpx.Request):
        assert "30ed029d7d0b2478a936e64657f9a999" in str(request.url)
        return httpx.Response(200, json=sample_trace)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await jaeger_get_trace(
            trace_id="30ed029d7d0b2478a936e64657f9a999",
            client=client,
            base_url="http://mock-jaeger:16686",
        )

        assert result["found"] is True
        assert result["span_count"] == 1
        span = result["spans"][0]
        assert span["service_name"] == "ad"
        assert span["status"]["code"] == 2
        assert span["duration_ms"] == 50.0


@pytest.mark.asyncio
async def test_jaeger_get_trace_not_found():
    async def mock_handler(request: httpx.Request):
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await jaeger_get_trace(
            trace_id="30ed029d7d0b2478a936e64657f9a999",
            client=client,
            base_url="http://mock-jaeger:16686",
        )
        assert result["found"] is False


def test_register_jaeger_tools():
    app = FastMCP("test-mcp")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "jaeger_search_traces" in tools
    assert "jaeger_get_trace" in tools

