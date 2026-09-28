"""OmniRoute behind a claude.ai-compatible URL.

OmniRoute (diegosouzapw/OmniRoute) already has a full MCP server — 110 tools, served over
Streamable HTTP at /api/mcp/stream by the OmniRoute app itself — so nothing is rewritten
here. Two things stop claude.ai from using it directly:

- the endpoint only accepts `Authorization: Bearer <key with manage scope>` from
  non-loopback clients, and a claude.ai custom connector cannot send a static header;
- MCP is disabled by default and must be switched to the streamable-http transport.

This module is a transparent streaming reverse proxy that injects the key, and a one-time
bootstrap that performs the dashboard steps through OmniRoute's own API (login, enable
MCP on streamable-http, create a manage-scoped key) when OMNIROUTE_PASSWORD is set.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

UPSTREAM = os.environ.get("OMNIROUTE_URL", "").rstrip("/")
PASSWORD = os.environ.get("OMNIROUTE_PASSWORD", "")
KEY_FILE = Path(os.environ.get("DATA_DIR", "/data")) / "omniroute" / "api_key"
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control", "x-accel-buffering")

_client: httpx.AsyncClient | None = None
_key: str | None = None
_ready_lock = asyncio.Lock()


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=None, write=60, pool=15))
    return _client


async def bootstrap(force: bool = False, rotate: bool = False) -> str:
    """Return a manage-scoped API key, enabling OmniRoute's MCP transport on first use.

    force: redo the setup (MCP was switched off); rotate: also mint a new key (old one rejected).
    """
    global _key
    async with _ready_lock:
        if _key and not force:
            return _key
        key = os.environ.get("OMNIROUTE_API_KEY", "").strip()
        if not key and KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
        if PASSWORD:
            async with httpx.AsyncClient(base_url=UPSTREAM, timeout=60) as c:
                r = await c.post("/api/auth/login", json={"password": PASSWORD})
                r.raise_for_status()
                r = await c.patch("/api/settings", json={"mcpEnabled": True, "mcpTransport": "streamable-http"})
                r.raise_for_status()
                if not key or rotate:
                    r = await c.post("/api/keys", json={"name": "claude-ai-connector", "scopes": ["manage"]})
                    r.raise_for_status()
                    key = r.json()["key"]
                    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                    KEY_FILE.write_text(key)
                    KEY_FILE.chmod(0o600)
        if not key:
            raise RuntimeError("set OMNIROUTE_PASSWORD (auto setup) or OMNIROUTE_API_KEY (manage scope)")
        _key = key
        return key


async def _forward(request: Request, key: str) -> httpx.Response:
    headers = {h: request.headers[h] for h in FORWARD_REQUEST_HEADERS if h in request.headers}
    headers["authorization"] = f"Bearer {key}"
    body = await request.body()
    req = client().build_request(request.method, f"{UPSTREAM}/api/mcp/stream", headers=headers, content=body)
    return await client().send(req, stream=True)


async def handle(request: Request) -> Response:
    if not UPSTREAM:
        return JSONResponse({"error": "OMNIROUTE_URL is not configured on this server"}, status_code=503)
    try:
        key = await bootstrap()
        upstream = await _forward(request, key)
        if upstream.status_code in (400, 401, 403, 503) and PASSWORD:
            body = await upstream.aread()
            await upstream.aclose()
            rejected_key = upstream.status_code in (401, 403)
            if not rejected_key and b"MCP" not in body:
                return Response(body, status_code=upstream.status_code,
                                media_type=upstream.headers.get("content-type"))
            # key revoked, or MCP switched off in the dashboard: redo the setup once
            key = await bootstrap(force=True, rotate=rejected_key)
            upstream = await _forward(request, key)
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        return JSONResponse({"error": f"OmniRoute unreachable or not set up: {exc}"}, status_code=502)
    headers = {h: upstream.headers[h] for h in FORWARD_RESPONSE_HEADERS if h in upstream.headers}
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers,
                             background=BackgroundTask(upstream.aclose))
