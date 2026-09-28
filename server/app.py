"""One process, four remote MCP endpoints usable as claude.ai custom connectors.

    https://<host>/<MCP_SECRET>/ponytail/mcp
    https://<host>/<MCP_SECRET>/agent-skills/mcp
    https://<host>/<MCP_SECRET>/graphify/mcp
    https://<host>/<MCP_SECRET>/omniroute/mcp

claude.ai custom connectors cannot send a static auth header, so access control is the
unguessable MCP_SECRET path segment (a capability URL). Treat the URLs like passwords.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import sys

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from . import agent_skills, graphify_server, omniroute_proxy, ponytail

SECRET = os.environ.get("MCP_SECRET", "")
if len(SECRET) < 24 and os.environ.get("ALLOW_WEAK_SECRET") != "1":
    sys.exit("MCP_SECRET must be set to a random string of at least 24 characters "
             "(python -c 'import secrets; print(secrets.token_urlsafe(32))')")

NO_REBINDING_CHECK = TransportSecuritySettings(enable_dns_rebinding_protection=False)
FASTMCP_SETTINGS = dict(stateless_http=True, transport_security=NO_REBINDING_CHECK)

pony = ponytail.build_server(**FASTMCP_SETTINGS)
skills = agent_skills.build_server(**FASTMCP_SETTINGS)
pony_app = pony.streamable_http_app()
skills_app = skills.streamable_http_app()
graph_manager = StreamableHTTPSessionManager(
    app=graphify_server.build_server(), stateless=True, security_settings=NO_REBINDING_CHECK
)


class _ManagerApp:
    """Raw ASGI endpoint for a session manager (same pattern as graphify.serve)."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)


class SecretGate:
    """404 for every path that does not start with /<MCP_SECRET>/ (constant-time compare)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            public = path in ("/", "/healthz")
            head = path.split("/", 2)[1] if path.count("/") >= 2 else ""
            if not public and not hmac.compare_digest(head.encode(), SECRET.encode()):
                await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def index(_: Request) -> JSONResponse:
    return JSONResponse({"service": "tokens_max MCP connectors",
                         "endpoints": ["ponytail", "agent-skills", "graphify", "omniroute"]})


async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with pony.session_manager.run(), skills.session_manager.run(), graph_manager.run():
        yield
    if omniroute_proxy._client is not None:
        await omniroute_proxy._client.aclose()


app = SecretGate(Starlette(
    routes=[
        Route("/", index),
        Route("/healthz", healthz),
        Mount(f"/{SECRET}", routes=[
            Mount("/ponytail", app=pony_app),
            Mount("/agent-skills", app=skills_app),
            Route("/graphify/mcp", _ManagerApp(graph_manager), methods=["GET", "POST", "DELETE"]),
            Route("/omniroute/mcp", omniroute_proxy.handle, methods=["GET", "POST", "DELETE"]),
        ]),
    ],
    lifespan=lifespan,
))


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
