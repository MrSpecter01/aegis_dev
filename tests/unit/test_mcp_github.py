import pytest
import httpx
from mcp.server.fastmcp import FastMCP

from services.mcp.tools.github import (
    github_get_commits,
    validate_repo,
    validate_iso_timestamp,
    register_tools,
)


@pytest.mark.asyncio
async def test_github_get_commits_success(mocker):
    mock_response = [
        {
            "sha": "abcdef1234567890abcdef1234567890abcdef12",
            "commit": {
                "message": "fix: resolve memory leak in cart service cache",
                "author": {
                    "name": "SRE Engineer",
                    "email": "sre@example.com",
                    "date": "2026-09-21T11:30:00Z",
                },
            },
        }
    ]

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 200
    mock_resp_obj.json.return_value = mock_response
    mock_resp_obj.raise_for_status.return_value = None
    mock_client.get.return_value = mock_resp_obj

    res = await github_get_commits(
        repo="GoogleCloudPlatform/microservices-demo",
        since="2026-09-21T10:00:00Z",
        until="2026-09-21T12:00:00Z",
        client=mock_client,
        base_url="https://api.github.com",
        headers={"Authorization": "Bearer mock-token"},
    )

    assert res["repo"] == "GoogleCloudPlatform/microservices-demo"
    assert res["found"] is True
    assert res["commit_count"] == 1
    assert res["commits"][0]["sha"] == "abcdef1"
    assert "memory leak" in res["commits"][0]["message"]
    assert res["commits"][0]["author"] == "SRE Engineer"


@pytest.mark.asyncio
async def test_github_get_commits_validation():
    with pytest.raises(ValueError, match="cannot be empty"):
        await github_get_commits(repo="", since="2026-09-21T10:00:00Z", until="2026-09-21T12:00:00Z")

    with pytest.raises(ValueError, match="Invalid repository format"):
        await github_get_commits(repo="invalid-repo-no-owner", since="2026-09-21T10:00:00Z", until="2026-09-21T12:00:00Z")

    with pytest.raises(ValueError, match="Invalid since timestamp"):
        await github_get_commits(repo="owner/repo", since="not-a-date", until="2026-09-21T12:00:00Z")

    with pytest.raises(ValueError, match="Invalid until timestamp"):
        await github_get_commits(repo="owner/repo", since="2026-09-21T10:00:00Z", until="not-a-date")


@pytest.mark.asyncio
async def test_github_get_commits_not_found(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 404
    mock_client.get.return_value = mock_resp_obj

    res = await github_get_commits(
        repo="owner/unknown-repo",
        since="2026-09-21T10:00:00Z",
        until="2026-09-21T12:00:00Z",
        client=mock_client,
        base_url="https://api.github.com",
    )
    assert res["found"] is False
    assert res["commit_count"] == 0
    assert "not found" in res["message"]


@pytest.mark.asyncio
async def test_github_get_commits_rate_limited(mocker):
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp_obj = mocker.MagicMock()
    mock_resp_obj.status_code = 403
    mock_resp_obj.text = "API rate limit exceeded"
    mock_client.get.return_value = mock_resp_obj

    res = await github_get_commits(
        repo="owner/repo",
        since="2026-09-21T10:00:00Z",
        until="2026-09-21T12:00:00Z",
        client=mock_client,
        base_url="https://api.github.com",
    )
    assert res["found"] is True
    assert res["commit_count"] == 0
    assert "rate limit exceeded" in res["message"]


def test_github_tool_registration():
    app = FastMCP("test-server")
    register_tools(app)
    tools = [t.name for t in app._tool_manager.list_tools()]
    assert "github_get_commits" in tools
