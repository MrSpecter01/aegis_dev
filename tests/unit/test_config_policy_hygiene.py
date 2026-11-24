import ast
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

from shared.harness.embeddings import generate_embedding
from shared.harness.sag_guard import SAGGuard
from shared.config.schemas import SAGPoliciesConfig, ToolPolicy


def test_no_nonempty_getenv_defaults():
    """Ensure no os.getenv call in services/, shared/, scripts/ has hardcoded non-empty defaults."""
    repo_root = Path(__file__).parent.parent.parent
    targets = [repo_root / "services", repo_root / "shared", repo_root / "scripts"]
    allowed_sentinels = {None, "", "0"}

    violations = []

    for target_dir in targets:
        if not target_dir.exists():
            continue
        for py_path in target_dir.rglob("*.py"):
            source = py_path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_path))
            except Exception as e:
                violations.append(f"{py_path}: Parse error: {e}")
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    is_os_getenv = False
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "getenv":
                        if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                            is_os_getenv = True

                    if is_os_getenv:
                        default_val = None
                        has_default = False
                        if len(node.args) >= 2:
                            has_default = True
                            arg2 = node.args[1]
                            if isinstance(arg2, ast.Constant):
                                default_val = arg2.value
                            else:
                                default_val = ast.unparse(arg2)
                        for kw in node.keywords:
                            if kw.arg == "default":
                                has_default = True
                                if isinstance(kw.value, ast.Constant):
                                    default_val = kw.value.value
                                else:
                                    default_val = ast.unparse(kw.value)

                        if has_default and default_val not in allowed_sentinels:
                            violations.append(
                                f"{py_path.relative_to(repo_root)}:{node.lineno}: os.getenv default={default_val!r} not in {allowed_sentinels}"
                            )

    assert not violations, "Found os.getenv calls with non-sentinel defaults:\n" + "\n".join(violations)


@pytest.mark.asyncio
async def test_embeddings_require_config(monkeypatch):
    """Test that embeddings functions fail closed when credentials or config are missing."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        await generate_embedding("test text", provider="openai")

    with pytest.raises(ValueError, match="GOOGLE_API_KEY is required"):
        await generate_embedding("test text", provider="gemini")


def test_sag_guard_policy_matching():
    """Test SAGGuard is_write_tool exact, prefix, substring, and unknown tool semantics."""
    guard = SAGGuard()

    # Read tools should not require approval
    assert guard.is_write_tool("prometheus_query") is False
    assert guard.is_write_tool("get_pod_status") is False
    assert guard.is_write_tool("topology_query_node") is False

    # Exact write tools should require approval
    assert guard.is_write_tool("restart_pod") is True
    assert guard.is_write_tool("scale_deployment") is True

    # Prefixed write tools (e.g. MCP-scoped kubernetes_restart_pod)
    assert guard.is_write_tool("kubernetes_restart_pod") is True
    assert guard.is_write_tool("mcp_tools_scale_deployment") is True

    # Substring non-match: restart_pod_status must NOT match restart_pod
    # Because restart_pod_status is unknown, it defaults to default_requires_approval (True)
    # But with a custom policy where unknown is False, restart_pod_status would be False
    custom_sag = SAGPoliciesConfig(
        default_requires_approval=False,
        tools={
            "restart_pod": ToolPolicy(requires_approval=True),
        },
    )
    mock_cm = MagicMock()
    mock_cm.sag = custom_sag
    with patch("shared.config.loader.ConfigManager", return_value=mock_cm):
        # restart_pod matches
        assert guard.is_write_tool("restart_pod") is True
        assert guard.is_write_tool("kubernetes_restart_pod") is True
        # restart_pod_status does not end with _restart_pod or equal restart_pod
        assert guard.is_write_tool("restart_pod_status") is False

    # Unknown tool with default_requires_approval=True (fail-closed)
    assert guard.is_write_tool("unregistered_random_tool") is True
