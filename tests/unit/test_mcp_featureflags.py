import json
import httpx
import pytest
from unittest.mock import AsyncMock
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.featureflags import get_feature_flag, toggle_feature_flag, register_tools


SAMPLE_FLAGD_CONFIG = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        "cartFailure": {
            "defaultVariant": "100%",
            "description": "Fail cart service n% of the time",
            "state": "ENABLED",
            "variants": {"off": 0, "100%": 1},
        },
        "adFailure": {
            "defaultVariant": "off",
            "description": "Fail ad service",
            "state": "ENABLED",
            "variants": {"off": False, "on": True},
        },
    },
}


@pytest.fixture
def mock_approved_redis():
    mock = AsyncMock()
    mock.get.return_value = json.dumps({"status": "approved", "action_id": "act-flag-01"})
    return mock


@pytest.fixture
def mock_unapproved_redis():
    mock = AsyncMock()
    mock.get.return_value = json.dumps({"status": "pending", "action_id": "act-flag-01"})
    return mock


@pytest.mark.asyncio
async def test_get_feature_flag_all_and_single():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://flagd.test/api/read-file"
        return httpx.Response(200, json=SAMPLE_FLAGD_CONFIG)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res_all = await get_feature_flag(flagd_url="http://flagd.test", client=client)
        assert res_all["success"] is True
        assert res_all["total_flags"] == 2
        assert "cartFailure" in res_all["active_flags"]
        assert "adFailure" not in res_all["active_flags"]

        res_one = await get_feature_flag(flag_name="cartFailure", flagd_url="http://flagd.test", client=client)
        assert res_one["success"] is True
        assert res_one["flag"]["defaultVariant"] == "100%"


@pytest.mark.asyncio
async def test_toggle_flag_valid_approved(mock_approved_redis):
    written_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=json.loads(json.dumps(SAMPLE_FLAGD_CONFIG)))
        if request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            written_payloads.append(body)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await toggle_feature_flag(
            flag_name="cartFailure",
            enabled=False,
            environment="otel-demo",
            incident_id="inc-1",
            action_id="act-flag-01",
            redis_client=mock_approved_redis,
            flagd_url="http://flagd.test",
            client=client,
        )
    assert res["success"] is True
    assert res["flag_name"] == "cartFailure"
    assert res["enabled"] is False
    assert res["previous_variant"] == "100%"
    assert res["variant"] == "off"
    assert res["environment"] == "otel-demo"
    assert len(written_payloads) == 1
    assert written_payloads[0]["data"]["flags"]["cartFailure"]["defaultVariant"] == "off"


@pytest.mark.asyncio
async def test_toggle_flag_invalid_charset(mock_approved_redis):
    res = await toggle_feature_flag(
        flag_name="flag name with spaces",
        enabled=False,
        environment="production",
        incident_id="inc-1",
        action_id="act-flag-01",
        redis_client=mock_approved_redis,
        flagd_url="",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "Must match pattern" in res["error"]


@pytest.mark.asyncio
async def test_toggle_flag_environment_not_whitelisted(mock_approved_redis):
    res = await toggle_feature_flag(
        flag_name="cartFailure",
        enabled=False,
        environment="chaos-sandbox",
        incident_id="inc-1",
        action_id="act-flag-01",
        redis_client=mock_approved_redis,
        flagd_url="",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "is not permitted" in res["error"]


@pytest.mark.asyncio
async def test_toggle_flag_not_approved(mock_unapproved_redis):
    res = await toggle_feature_flag(
        flag_name="cartFailure",
        enabled=False,
        environment="production",
        incident_id="inc-1",
        action_id="act-flag-01",
        redis_client=mock_unapproved_redis,
        flagd_url="",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "not approved" in res["error"]


@pytest.mark.asyncio
async def test_toggle_flag_injection(mock_approved_redis):
    res = await toggle_feature_flag(
        flag_name="flag; rm -rf /",
        enabled=False,
        environment="production",
        incident_id="inc-1",
        action_id="act-flag-01",
        redis_client=mock_approved_redis,
        flagd_url="",
    )
    assert res["success"] is False
    assert res["isError"] is True


def test_featureflags_tool_registration():
    app = FastMCP("test-ff-mcp")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "toggle_feature_flag" in tools
    assert "get_feature_flag" in tools


@pytest.mark.asyncio
async def test_toggle_flag_unconfigured_url_fails_closed(mock_approved_redis, monkeypatch):
    monkeypatch.delenv("AEGIS_FLAGD_URL", raising=False)
    monkeypatch.delenv("FLAGD_UI_URL", raising=False)
    res = await toggle_feature_flag(
        flag_name="cartFailure",
        enabled=False,
        environment="otel-demo",
        incident_id="inc-1",
        action_id="act-flag-01",
        redis_client=mock_approved_redis,
        flagd_url="",
    )
    assert res["success"] is False
    assert res["isError"] is True
    assert "flagd_url is not configured" in res["error"]
