#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
HTTP MCP server for SAS Viya with direct (service-account) authentication.

Unlike the standard HTTP server (``app``), which requires each MCP client to
complete a browser-based OAuth flow, this server authenticates to Viya itself
with the credentials from the environment — a refresh token or the password
grant — and serves the MCP protocol over streamable HTTP (or SSE).

This is the mode for MCP clients that cannot perform interactive OAuth, such
as SAS Retrieval Agent Manager (RAM). The endpoint can be protected with a
static API key by setting ``MCP_API_KEY``; clients then send it as an
``X-API-Key`` header or an ``Authorization: Bearer`` token.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import client_request, select_grant
from .config import CLIENT_ID, CLIENT_SECRET, HOST_PORT, SERVER_NAME, SSL_VERIFY, VIYA_ENDPOINT
from .exceptions import AuthenticationError
from .prompts import register_prompts
from .tools import register_tools
from .version import server_version
from .viya_client import announce_startup, logger
from .viya_utils import shutdown_session_cache

load_dotenv()

__all__ = ["ApiKeyMiddleware", "AuthenticationError", "build_app", "get_viya_token", "main", "mcp"]

VIYA_USERNAME = os.getenv("VIYA_USERNAME", "")
VIYA_PASSWORD = os.getenv("VIYA_PASSWORD", "")
# Preferred for SSO / federated (e.g. Okta) environments and for unattended
# 24/7 deployments: a refresh token obtained once via an interactive login
# (see examples/get_refresh_token.py). When set it is used in preference to
# username/password and carries the full identity of the user who issued it.
VIYA_REFRESH_TOKEN = os.getenv("VIYA_REFRESH_TOKEN", "")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")
# "http" (streamable HTTP, endpoint /mcp) or "sse" (Server-Sent Events,
# endpoint /sse) — matching the transport selected in the MCP client.
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "http").lower()

# Refresh the cached token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60.0

# "refresh_token" holds the most recent refresh token in use. It is seeded
# lazily from VIYA_REFRESH_TOKEN and updated if SAS Logon rotates it.
_token_cache = {"token": "", "expires_at": 0.0, "refresh_token": ""}
# Serialises token refresh so a burst of concurrent requests at expiry doesn't
# stampede /SASLogon or race to consume a rotating refresh token.
_token_lock = asyncio.Lock()


async def get_viya_token() -> str:
    """Return a Viya access token, with caching.

    Uses the refresh_token grant when ``VIYA_REFRESH_TOKEN`` is set, otherwise
    the password grant. The access token is cached until shortly before its
    expiry, so a busy agent does not hit /SASLogon on every tool call.
    """
    if _token_cache["token"] and time.monotonic() < _token_cache["expires_at"]:
        return _token_cache["token"]

    async with _token_lock:
        # Another coroutine may have refreshed while we waited for the lock.
        if _token_cache["token"] and time.monotonic() < _token_cache["expires_at"]:
            return _token_cache["token"]

        refresh_token = _token_cache["refresh_token"] or VIYA_REFRESH_TOKEN
        grant = select_grant(refresh_token=refresh_token, username=VIYA_USERNAME, password=VIYA_PASSWORD)
        if grant is None:
            raise AuthenticationError(
                "No Viya credentials configured for direct HTTP mode. Set VIYA_REFRESH_TOKEN "
                "(recommended for SSO/federated environments) or VIYA_USERNAME and VIYA_PASSWORD."
            )
        data, auth = client_request(grant, CLIENT_ID, CLIENT_SECRET)
        async with httpx.AsyncClient(verify=SSL_VERIFY) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/SASLogon/oauth/token",
                auth=auth,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=data,
            )
        resp.raise_for_status()
        body = resp.json()
        _token_cache["token"] = body["access_token"]
        expires_in = float(body.get("expires_in", 0))
        _token_cache["expires_at"] = time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN, 0.0)
        # Honour refresh-token rotation: if SAS Logon returned a new refresh
        # token, use it for the next refresh so the chain does not break.
        if body.get("refresh_token"):
            _token_cache["refresh_token"] = body["refresh_token"]
        return _token_cache["token"]


async def _direct_get_token(ctx: Context) -> str:
    return await get_viya_token()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Tear down warm compute sessions when the server stops."""
    try:
        yield {}
    finally:
        await shutdown_session_cache()


SERVER_VERSION = server_version()
announce_startup("http-direct", SERVER_VERSION)
mcp = FastMCP(SERVER_NAME, version=SERVER_VERSION, lifespan=_lifespan)

register_tools(mcp, _direct_get_token)
register_prompts(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "sas-mcp-usecase"})


class ApiKeyMiddleware:
    """ASGI middleware that rejects HTTP requests lacking the API key.

    Accepts the key via ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
    ``/health`` stays open so liveness probes work without credentials.
    """

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        provided = headers.get("x-api-key", "")
        if not provided:
            auth = headers.get("authorization", "")
            parts = auth.split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                provided = parts[1]
        if provided != self.api_key:
            response = JSONResponse({"error": "invalid or missing API key"}, status_code=401)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def build_app():
    """Build the ASGI app, wrapping with API key auth when configured."""
    if MCP_TRANSPORT not in ("http", "sse"):
        raise ValueError(f"MCP_TRANSPORT must be 'http' or 'sse', got '{MCP_TRANSPORT}'")
    logger.info(
        "Serving MCP over '%s' transport (endpoint: /%s)", MCP_TRANSPORT, "mcp" if MCP_TRANSPORT == "http" else "sse"
    )
    app = mcp.http_app(transport=MCP_TRANSPORT)
    if MCP_API_KEY:
        logger.info("API key protection enabled (MCP_API_KEY is set)")
        return ApiKeyMiddleware(app, MCP_API_KEY)
    logger.warning(
        "MCP_API_KEY is not set — the MCP endpoint is unauthenticated. "
        "Anyone who can reach it can act on Viya as the configured user."
    )
    return app


def main() -> None:
    """Run the MCP server over streamable HTTP with direct Viya auth."""
    uvicorn.run(build_app(), host="0.0.0.0", port=HOST_PORT)


if __name__ == "__main__":
    main()
