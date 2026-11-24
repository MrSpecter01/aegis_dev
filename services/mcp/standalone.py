import argparse
import os
from typing import Callable, Dict, List, Optional
import urllib.parse
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from shared.config.loader import ConfigManager
from services.mcp.tools import (
    prometheus,
    loki,
    tempo,
    jaeger,
    kubernetes,
    kubernetes_write,
    featureflags,
    argocd,
    github,
    qdrant,
)
from services.mcp.topology import server as topology_server

DOMAIN_REGISTRARS: Dict[str, Callable[[FastMCP], None]] = {
    "prometheus": prometheus.register_tools,
    "loki": loki.register_tools,
    "tempo": tempo.register_tools,
    "jaeger": jaeger.register_tools,
    "kubernetes": kubernetes.register_tools,
    "kubernetes_write": kubernetes_write.register_write_tools,
    "featureflags": featureflags.register_tools,
    "argocd": argocd.register_tools,
    "github": github.register_tools,
    "topology": topology_server.register_tools,
    "qdrant": qdrant.register_tools,
}

DEFAULT_PORTS: Dict[str, int] = {
    "prometheus": 9001,
    "loki": 9002,
    "tempo": 9003,
    "kubernetes": 9004,
    "kubernetes_write": 9004,
    "argocd": 9005,
    "github": 9006,
    "featureflags": 9007,
    "topology": 9008,
    "jaeger": 9009,
    "qdrant": 9010,
}


def get_domain_port(domain: str) -> int:
    """Resolve port for a domain from config/mcp.yaml or default."""
    try:
        config = ConfigManager()
        if domain in config.mcp.mcp_servers:
            server_url = config.mcp.mcp_servers[domain].server_url
            parsed = urllib.parse.urlparse(server_url)
            if parsed.port:
                return parsed.port
    except Exception as exc:
        logger.warning("Failed to resolve port for domain %s from config: %s", domain, exc)
    return DEFAULT_PORTS.get(domain, 9000)


def create_standalone_mcp(service: str, host: str = "0.0.0.0", port: Optional[int] = None) -> FastMCP:
    """Create a FastMCP instance for a single domain in isolated mode."""
    domain = service.lower().strip()
    if domain not in DOMAIN_REGISTRARS:
        raise ValueError(
            f"Unknown standalone service '{service}'. Available services: {list(DOMAIN_REGISTRARS.keys())}"
        )

    resolved_port = port or get_domain_port(domain)
    app = FastMCP(f"{domain}-mcp", host=host, port=resolved_port)

    # Register only this domain's tools
    registrar = DOMAIN_REGISTRARS[domain]
    registrar(app)

    @app.custom_route("/health", methods=["GET"])
    async def health_check(request):
        tools_list: List[str] = [t.name for t in app._tool_manager.list_tools()]
        return JSONResponse(
            {
                "status": "ok",
                "service": f"{domain}-mcp",
                "domain": domain,
                "tool_count": len(tools_list),
            },
            status_code=200,
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="Aegis Standalone FastMCP Server Runner")
    parser.add_argument(
        "--service",
        type=str,
        required=True,
        help=f"Service domain to run ({', '.join(DOMAIN_REGISTRARS.keys())})",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Binding host")
    parser.add_argument("--port", type=int, default=None, help="Binding port (overrides config)")
    args = parser.parse_args()

    app = create_standalone_mcp(service=args.service, host=args.host, port=args.port)
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
