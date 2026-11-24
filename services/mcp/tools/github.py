from datetime import datetime
import re
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.config.loader import ConfigManager

REPO_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


def validate_repo(repo: str) -> None:
    if not repo or not repo.strip():
        raise ValueError("Repository cannot be empty")
    cleaned = repo.strip()
    if len(cleaned) > 100:
        raise ValueError(f"Repository name too long: '{repo}'")
    if not REPO_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid repository format: '{repo}'. Must be 'owner/repo' (alphanumeric, dots, dashes, underscores)."
        )


def validate_iso_timestamp(ts: str, field_name: str) -> None:
    if not ts or not ts.strip():
        raise ValueError(f"{field_name} timestamp cannot be empty")
    cleaned = ts.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(cleaned)
    except ValueError as e:
        raise ValueError(
            f"Invalid {field_name} timestamp: '{ts}'. Expected ISO8601 format (e.g., '2026-09-21T10:00:00Z')"
        ) from e


def _get_github_config() -> tuple[str, Dict[str, str]]:
    config = ConfigManager()
    token = config.system.mcp_endpoints.github_token
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Aegis-SRE-Agent",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_url = "https://api.github.com"
    return api_url, headers


async def github_get_commits(
    repo: str,
    since: str,
    until: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Retrieve commits from GitHub repository within a time window [since, until].
    """
    validate_repo(repo)
    validate_iso_timestamp(since, "since")
    validate_iso_timestamp(until, "until")

    if base_url is None:
        resolved_url, resolved_headers = _get_github_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/repos/{repo}/commits"
    params = {
        "since": since,
        "until": until,
        "per_page": 50,
    }

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, params=params, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "repo": repo,
                    "found": False,
                    "since": since,
                    "until": until,
                    "commit_count": 0,
                    "commits": [],
                    "message": f"Repository '{repo}' not found on GitHub",
                }
            if resp.status_code == 403:
                return {
                    "repo": repo,
                    "found": True,
                    "since": since,
                    "until": until,
                    "commit_count": 0,
                    "commits": [],
                    "message": f"GitHub API rate limit exceeded or access forbidden: {resp.text}",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"GitHub HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"GitHub connection error: {str(e)}") from e

        commits: List[Dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                commit_info = item.get("commit", {})
                author_info = commit_info.get("author", {}) or {}
                commits.append({
                    "sha": item.get("sha", "")[:7],
                    "full_sha": item.get("sha", ""),
                    "message": commit_info.get("message", "").strip(),
                    "author": author_info.get("name", "unknown"),
                    "author_email": author_info.get("email", ""),
                    "timestamp": author_info.get("date", ""),
                })

        return {
            "repo": repo,
            "found": True,
            "since": since,
            "until": until,
            "commit_count": len(commits),
            "commits": commits,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient() as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register GitHub tools onto a FastMCP instance."""
    @mcp.tool(name="github_get_commits")
    async def mcp_github_get_commits(repo: str, since: str, until: str) -> Dict[str, Any]:
        """Retrieve recent commits from a GitHub repository between since and until ISO8601 timestamps."""
        return await github_get_commits(repo, since, until)
