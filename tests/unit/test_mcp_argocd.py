import pytest
import httpx
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.argocd import (
    argocd_get_app_status,
    argocd_get_sync_history,
    validate_app_name,
    validate_limit,
    register_tools,
)


@pytest.mark.asyncio
async def test_argocd_get_app_status_success(mocker):
    mock_response = {
        "metadata": {"name": "boutique"},
        "spec": {
            "source": {
                "repoURL": "https://github.com/GoogleCloudPlatform/microservices-demo",
                "targetRevision": "main",
                "path": "release",
            }
        },
        "status": {
            "health": {"status": "Healthy", "message": "all pods ready"},
            "sync": {"status": "Synced", "revision": "a1b2c3d4e5f6"},
        },
    }

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_response
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await argocd_get_app_status(
        app="boutique",
        client=mock_client,
        base_url="http://mock-argocd:8080",
        headers={"Authorization": "Bearer test-token"},
    )

    assert res["app"] == "boutique"
    assert res["configured"] is True
    assert res["found"] is True
    assert res["health"] == "Healthy"
    assert res["sync"] == "Synced"
    assert res["sync_revision"] == "a1b2c3d4e5f6"
    assert res["repo_url"] == "https://github.com/GoogleCloudPlatform/microservices-demo"


@pytest.mark.asyncio
async def test_argocd_get_app_status_not_configured():
    res = await argocd_get_app_status(
        app="boutique",
        base_url="",
    )
    assert res["configured"] is False
    assert res["health"] == "unknown"
    assert "not configured" in res["message"]


@pytest.mark.asyncio
async def test_argocd_get_app_status_not_found(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 404
    mock_client.get.return_value = mock_resp_obj

    res = await argocd_get_app_status(
        app="non-existent-app",
        client=mock_client,
        base_url="http://mock-argocd:8080",
    )
    assert res["found"] is False
    assert res["health"] == "not_found"
    assert "not found" in res["message"]


@pytest.mark.asyncio
async def test_argocd_get_sync_history_success(mocker):
    mock_response = {
        "status": {
            "history": [
                {
                    "id": 1,
                    "revision": "commit-111",
                    "deployedAt": "2026-09-21T10:00:00Z",
                    "source": {"targetRevision": "v1.0.0"},
                },
                {
                    "id": 2,
                    "revision": "commit-222",
                    "deployedAt": "2026-09-21T11:00:00Z",
                    "source": {"targetRevision": "v1.1.0"},
                },
            ]
        }
    }

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_response
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await argocd_get_sync_history(
        app="boutique",
        limit=5,
        client=mock_client,
        base_url="http://mock-argocd:8080",
    )

    assert res["app"] == "boutique"
    assert res["found"] is True
    assert res["sync_count"] == 2
    # Check sorted descending by id
    assert res["history"][0]["id"] == 2
    assert res["history"][0]["revision"] == "commit-222"


@pytest.mark.asyncio
async def test_argocd_validation():
    with pytest.raises(ValueError, match="cannot be empty"):
        await argocd_get_app_status(app="")

    with pytest.raises(ValueError, match="Invalid ArgoCD application name"):
        await argocd_get_app_status(app="app; dangerous")

    with pytest.raises(ValueError, match="limit must be at least 1"):
        await argocd_get_sync_history(app="boutique", limit=0)


def test_argocd_tool_registration():
    app = FastMCP("test-server")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "argocd_get_app_status" in tools
    assert "argocd_get_sync_history" in tools
