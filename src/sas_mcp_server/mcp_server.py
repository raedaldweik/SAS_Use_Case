#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
HTTP MCP server for SAS Viya with per-user OAuth 2.0 (authorization code + PKCE).

Each MCP client signs in through SAS Logon in the browser; the proxy JWT it
then presents is swapped for the user's own Viya access token on every call.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import SERVER_NAME, viya_auth
from .exceptions import AuthenticationError
from .prompts import register_prompts
from .tools import register_tools
from .version import server_version
from .viya_client import announce_startup, logger
from .viya_utils import shutdown_session_cache

# Load environment variables before accessing them
load_dotenv()

__all__ = ["AuthenticationError", "app", "mcp"]


class AuthMiddleware(Middleware):
    async def on_call_tool(self, ctx: MiddlewareContext, call_next: Any) -> Any:
        request = get_http_request()
        bearer_token = request.headers.get("Authorization")
        if not bearer_token:
            logger.error("No auth header found. Cannot proceed")
            raise AuthenticationError("No auth header found. Cannot proceed")

        parts = bearer_token.split()
        jwt = parts[1] if len(parts) > 1 and parts[0].lower() == "bearer" else bearer_token
        logger.info("Client auth header found, swapping for upstream token")
        viya_access_info = await viya_auth.load_access_token(jwt)
        if viya_access_info:
            logger.info("Viya access info retrieved successfully")
            fastmcp_ctx = ctx.fastmcp_context
            if fastmcp_ctx is not None:
                await fastmcp_ctx.set_state("access_token", viya_access_info.token)
        else:
            logger.error("Could not retrieve upstream access token!")
        return await call_next(ctx)


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Tear down warm compute sessions when the server stops."""
    try:
        yield {}
    finally:
        await shutdown_session_cache()


SERVER_VERSION = server_version()
announce_startup("http", SERVER_VERSION)
mcp = FastMCP(SERVER_NAME, version=SERVER_VERSION, auth=viya_auth, lifespan=_lifespan)
mcp.add_middleware(AuthMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "sas-mcp-usecase"})


# Token getter for HTTP mode: reads from context state set by AuthMiddleware
async def _http_get_token(ctx: Context) -> str:
    token = await ctx.get_state("access_token")
    if not token:
        raise AuthenticationError("No auth header found. Cannot authenticate to Viya")
    return token


register_tools(mcp, _http_get_token)
register_prompts(mcp)

app = mcp.http_app()
