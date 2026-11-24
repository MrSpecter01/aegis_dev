from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from shared.config import load_agents_config
from shared.harness import build_llm, build_mcp_client, get_mcp_client, get_mcp_clients, MCPClient
from shared.config.schemas import MCPServerConfig
from tests.helpers import FakeLLM


def test_build_llm_ollama():
    agents_cfg = load_agents_config()
    infra_cfg = agents_cfg.agents["infrastructure_investigator"]
    llm = build_llm(infra_cfg)
    assert llm is not None


def test_build_llm_by_agent_id():
    llm = build_llm("infrastructure_investigator")
    assert llm is not None


def test_build_llm_invalid_agent_id_raises():
    with pytest.raises(KeyError, match="not found in agents.yaml"):
        build_llm("nonexistent_agent")


def test_get_mcp_client_success():
    client = get_mcp_client("prometheus")
    assert isinstance(client, MCPClient)
    assert "9001" in client.server_url or "9000" in client.server_url


def test_get_mcp_client_invalid_raises():
    with pytest.raises(KeyError, match="not found in mcp.yaml"):
        get_mcp_client("nonexistent_server")


def test_get_mcp_clients_selective_and_all():
    clients = get_mcp_clients(["prometheus", "loki"])
    assert "prometheus" in clients
    assert "loki" in clients
    assert len(clients) == 2

    all_clients = get_mcp_clients()
    assert len(all_clients) >= 5



def test_fake_llm_generation():
    llm = FakeLLM(response_text="Hello Aegis")
    res = llm.invoke("Test prompt")
    assert res.content == "Hello Aegis"


def test_build_mcp_client():
    server_cfg = MCPServerConfig(server_url="http://localhost:9001/mcp", tools=["prometheus_query"])
    client = build_mcp_client(server_cfg)
    assert isinstance(client, MCPClient)
    assert client.server_url == "http://localhost:9001/mcp"


def test_mcp_client_auth_headers():
    client_no_auth = MCPClient("http://localhost:9001/mcp")
    assert client_no_auth._get_headers() is None

    client_with_auth = MCPClient("http://localhost:9001/mcp", api_key="secret-token")
    assert client_with_auth._get_headers() == {"Authorization": "Bearer secret-token"}


@pytest.mark.asyncio
async def test_mcp_client_list_tools_and_get_tool():
    client = MCPClient("http://localhost:9001/mcp")

    mock_session = AsyncMock()
    mock_tool = MagicMock()
    mock_tool.model_dump.return_value = {"name": "prometheus_query", "description": "query prometheus"}
    mock_list_res = MagicMock()
    mock_list_res.tools = [mock_tool]
    mock_session.list_tools.return_value = mock_list_res

    class MockStreamable:
        async def __aenter__(self):
            return (MagicMock(), MagicMock(), None)

        async def __aexit__(self, *args):
            pass

    class MockSessionCtx:
        def __init__(self, r, w):
            pass

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    with patch("shared.harness.mcp.streamablehttp_client", return_value=MockStreamable()):
        with patch("shared.harness.mcp.ClientSession", MockSessionCtx):
            tools = await client.list_tools()
            assert len(tools) == 1
            assert tools[0]["name"] == "prometheus_query"

            tool = await client.get_tool("prometheus_query")
            assert tool is not None
            assert tool["name"] == "prometheus_query"

            missing = await client.get_tool("nonexistent")
            assert missing is None


@pytest.mark.asyncio
async def test_mcp_client_call_tool():
    client = MCPClient("http://localhost:9001/mcp")

    mock_session = AsyncMock()
    mock_call_res = MagicMock()
    mock_call_res.model_dump.return_value = {
        "content": [{"type": "text", "text": "result-data"}],
        "isError": False,
    }
    mock_session.call_tool.return_value = mock_call_res

    class MockStreamable:
        async def __aenter__(self):
            return (MagicMock(), MagicMock(), None)

        async def __aexit__(self, *args):
            pass

    class MockSessionCtx:
        def __init__(self, r, w):
            pass

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    with patch("shared.harness.mcp.streamablehttp_client", return_value=MockStreamable()):
        with patch("shared.harness.mcp.ClientSession", MockSessionCtx):
            res = await client.call_tool("prometheus_query", {"query": "up"})
            assert res["isError"] is False
            assert res["content"][0]["text"] == "result-data"
            mock_session.call_tool.assert_called_once_with("prometheus_query", {"query": "up"})


def test_resolve_mcp_client_for_tool():
    from shared.harness.mcp import resolve_mcp_client_for_tool

    # restart_pod is configured on kubernetes server
    k8s_client = resolve_mcp_client_for_tool("restart_pod")
    assert isinstance(k8s_client, MCPClient)
    assert "9004" in k8s_client.server_url

    # toggle_feature_flag is configured on featureflags server
    ff_client = resolve_mcp_client_for_tool("toggle_feature_flag")
    assert isinstance(ff_client, MCPClient)
    assert "9007" in ff_client.server_url

    # Unknown tool raises KeyError
    with pytest.raises(KeyError, match="No MCP server found"):
        from shared.config.schemas import MCPConfig
        resolve_mcp_client_for_tool("nonexistent_unknown_tool_xyz", config=MCPConfig())


@pytest.mark.asyncio
async def test_generate_embedding_multi_provider():
    from unittest.mock import AsyncMock, MagicMock, patch
    from shared.harness.embeddings import generate_embedding

    # 1. Custom embedder with aembed_query
    mock_async_embedder = AsyncMock()
    mock_async_embedder.aembed_query.return_value = [0.1, 0.2, 0.3]
    res = await generate_embedding("hello", client=mock_async_embedder)
    assert res == [0.1, 0.2, 0.3]
    mock_async_embedder.aembed_query.assert_awaited_once_with("hello")

    # 2. Custom embedder with sync embed_query
    mock_sync_embedder = MagicMock(spec=["embed_query"])
    mock_sync_embedder.embed_query.return_value = [0.4, 0.5, 0.6]
    res = await generate_embedding("hello", client=mock_sync_embedder)
    assert res == [0.4, 0.5, 0.6]
    mock_sync_embedder.embed_query.assert_called_once_with("hello")

    # 3. Provider dispatch through build_embeddings
    with patch("shared.harness.embeddings.build_embeddings") as mock_build:
        mock_build.return_value = mock_async_embedder
        res = await generate_embedding("hello", provider="ollama")
        assert res == [0.1, 0.2, 0.3]
        mock_build.assert_called_once_with(provider="ollama", model=None, base_url=None, api_key=None)

    # 4. Unsupported provider raises ValueError
    with pytest.raises(ValueError, match="Unsupported embedding provider"):
        await generate_embedding("hello", provider="unknown_provider")

    # 5. Invalid embedder instance raises TypeError
    with patch("shared.harness.embeddings.build_embeddings", return_value=object()):
        with pytest.raises(TypeError, match="must implement aembed_query or embed_query"):
            await generate_embedding("hello", provider="ollama")


def test_build_embeddings_langchain():
    from shared.harness.embeddings import build_embeddings
    from langchain_ollama import OllamaEmbeddings
    from langchain_openai import OpenAIEmbeddings
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    # 1. Ollama
    ollama_emb = build_embeddings("ollama")
    assert isinstance(ollama_emb, OllamaEmbeddings)
    assert hasattr(ollama_emb, "aembed_query")

    # 2. OpenAI
    openai_emb = build_embeddings("openai", api_key="test-key")
    assert isinstance(openai_emb, OpenAIEmbeddings)
    assert hasattr(openai_emb, "aembed_query")

    # 3. Gemini
    gemini_emb = build_embeddings("gemini", api_key="test-key")
    assert isinstance(gemini_emb, GoogleGenerativeAIEmbeddings)
    assert hasattr(gemini_emb, "aembed_query")

    # 4. Gemini with custom model identifier 'gemini-embedding-2'
    gemini_emb2 = build_embeddings("gemini", model="gemini-embedding-2", api_key="test-key")
    assert isinstance(gemini_emb2, GoogleGenerativeAIEmbeddings)
    assert gemini_emb2.model == "models/gemini-embedding-2"


def test_mcp_client_auth_variants_and_custom_headers():
    # api_key auth_type defaults to X-API-Key
    client_apikey = MCPClient(
        "https://mcp.example.com/mcp",
        api_key="my-key",
        auth_type="api_key",
        headers={"X-Tenant": "acme"},
    )
    assert client_apikey._get_headers() == {
        "X-Tenant": "acme",
        "X-API-Key": "my-key",
    }

    # custom auth_header via header auth_type
    client_custom = MCPClient(
        "https://mcp.example.com/mcp",
        api_key="tok-99",
        auth_type="header",
        auth_header="X-Custom-Auth",
    )
    assert client_custom._get_headers() == {"X-Custom-Auth": "tok-99"}

    # basic auth base64-encodes api_key
    client_basic = MCPClient(
        "https://mcp.example.com/mcp",
        api_key="alice:secret",
        auth_type="basic",
    )
    assert client_basic._get_headers() == {"Authorization": "Basic YWxpY2U6c2VjcmV0"}

    # none auth skips api_key but preserves extra headers
    client_none = MCPClient(
        "https://mcp.example.com/mcp",
        api_key="ignored",
        auth_type="none",
        headers={"X-Trace": "1"},
    )
    assert client_none._get_headers() == {"X-Trace": "1"}


@pytest.mark.asyncio
async def test_mcp_client_sse_and_stdio_transports():
    mock_session = AsyncMock()
    mock_tool = MagicMock()
    mock_tool.model_dump.return_value = {"name": "ext_tool", "description": "external"}
    mock_list_res = MagicMock()
    mock_list_res.tools = [mock_tool]
    mock_session.list_tools.return_value = mock_list_res

    mock_call_res = MagicMock()
    mock_call_res.model_dump.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
    }
    mock_session.call_tool.return_value = mock_call_res

    class MockTwoTupleTransport:
        async def __aenter__(self):
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *args):
            pass

    class MockSessionCtx:
        def __init__(self, r, w):
            pass

        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    # SSE transport
    sse_client_obj = MCPClient(
        "https://mcp.example.com/sse",
        transport="sse",
        api_key="sse-token",
        auth_type="bearer",
    )
    with patch("shared.harness.mcp.sse_client", return_value=MockTwoTupleTransport()) as mock_sse:
        with patch("shared.harness.mcp.ClientSession", MockSessionCtx):
            tools = await sse_client_obj.list_tools()
            assert tools[0]["name"] == "ext_tool"
            res = await sse_client_obj.call_tool("ext_tool", {"q": "cpu"})
            assert res["content"][0]["text"] == "ok"
            mock_sse.assert_called_with(
                url="https://mcp.example.com/sse",
                headers={"Authorization": "Bearer sse-token"},
                timeout=30.0,
            )

    # STDIO transport
    stdio_client_obj = MCPClient(
        transport="stdio",
        command="python",
        args=["-m", "fake_mcp"],
        env={"MCP_SECRET": "xyz"},
    )
    with patch("shared.harness.mcp.stdio_client", return_value=MockTwoTupleTransport()) as mock_stdio:
        with patch("shared.harness.mcp.ClientSession", MockSessionCtx):
            tools = await stdio_client_obj.list_tools()
            assert tools[0]["name"] == "ext_tool"
            res = await stdio_client_obj.call_tool("ext_tool", {"path": "."})
            assert res["isError"] is False
            params = mock_stdio.call_args[0][0]
            assert params.command == "python"
            assert params.args == ["-m", "fake_mcp"]
            assert params.env["MCP_SECRET"] == "xyz"


