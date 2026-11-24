import os
from typing import List
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from services.mcp.tools import (
    prometheus,
    loki,
    tempo,
    jaeger,
    kubernetes,
    argocd,
    github,
    kubernetes_write,
    featureflags,
    qdrant,
)
from services.mcp.topology import server as topology_server


def create_unified_mcp(host: str = "0.0.0.0", port: int = 9000) -> FastMCP:
    """Construct and configure the unified FastMCP server with all tool domains."""
    app = FastMCP("aegis-mcp", host=host, port=port)

    # Register all domain read and write tools
    prometheus.register_tools(app)
    loki.register_tools(app)
    tempo.register_tools(app)
    jaeger.register_tools(app)
    kubernetes.register_tools(app)
    kubernetes_write.register_write_tools(app)
    featureflags.register_tools(app)
    argocd.register_tools(app)
    github.register_tools(app)
    topology_server.register_tools(app)
    qdrant.register_tools(app)

    @app.custom_route("/health", methods=["GET"])
    async def health_check(request):
        """Active health check probe for unified aegis-mcp container."""
        tools_list: List[str] = [t.name for t in app._tool_manager.list_tools()]
        return JSONResponse(
            {
                "status": "ok",
                "service": "aegis-mcp",
                "tool_count": len(tools_list),
                "domains": [
                    "prometheus",
                    "loki",
                    "tempo",
                    "jaeger",
                    "kubernetes",
                    "kubernetes_write",
                    "featureflags",
                    "argocd",
                    "github",
                    "topology",
                    "qdrant",
                ],
            },
            status_code=200,
        )

    return app


from shared.config.loader import ConfigManager

_sys_cfg = ConfigManager().system
_host = os.getenv("AEGIS_MCP_HOST") or _sys_cfg.mcp_server.host
_port = int(os.getenv("AEGIS_MCP_PORT") or _sys_cfg.mcp_server.port)
mcp = create_unified_mcp(host=_host, port=_port)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
