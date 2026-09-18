#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Stdio MCP server for SAS Viya.

Authenticates to Viya itself — with a refresh token (``VIYA_REFRESH_TOKEN``,
required for SSO/federated users) or the password grant (``VIYA_USERNAME`` /
``VIYA_PASSWORD``) — so an MCP client can start the server on demand without a
pre-running HTTP server or a browser.
"""

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

from .auth import client_request, select_grant
from .config import CLIENT_ID, CLIENT_SECRET, SERVER_NAME, SSL_VERIFY, VIYA_ENDPOINT
from .exceptions import AuthenticationError
from .prompts import register_prompts
from .tools import register_tools
from .version import server_version
from .viya_client import announce_startup
from .viya_utils import shutdown_session_cache

load_dotenv()

__all__ = ["AuthenticationError", "main", "mcp"]

VIYA_USERNAME = os.getenv("VIYA_USERNAME", "")
VIYA_PASSWORD = os.getenv("VIYA_PASSWORD", "")
# Preferred for SSO / federated (e.g. Okta) environments: a refresh token
# obtained once via an interactive login (see examples/get_refresh_token.py).
VIYA_REFRESH_TOKEN = os.getenv("VIYA_REFRESH_TOKEN", "")

# Refresh the cached token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60.0
# "refresh_token" is seeded lazily from VIYA_REFRESH_TOKEN and updated if
# SAS Logon rotates it.
_token_cache = {"token": "", "expires_at": 0.0, "refresh_token": ""}


def _get_viya_token() -> str:
    """Return a Viya access token, with caching."""
    if _token_cache["token"] and time.monotonic() < _token_cache["expires_at"]:
        return _token_cache["token"]

    refresh_token = _token_cache["refresh_token"] or VIYA_REFRESH_TOKEN
    grant = select_grant(refresh_token=refresh_token, username=VIYA_USERNAME, password=VIYA_PASSWORD)
    if grant is None:
        raise AuthenticationError(
            "No Viya credentials configured for stdio mode. Set VIYA_REFRESH_TOKEN (required for "
            "SSO/federated environments) or VIYA_USERNAME and VIYA_PASSWORD in .env."
        )
    data, auth = client_request(grant, CLIENT_ID, CLIENT_SECRET)
    resp = httpx.post(
        f"{VIYA_ENDPOINT}/SASLogon/oauth/token",
        auth=auth,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
        verify=SSL_VERIFY,
    )
    resp.raise_for_status()
    body = resp.json()
    _token_cache["token"] = body["access_token"]
    expires_in = float(body.get("expires_in", 0))
    _token_cache["expires_at"] = time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN, 0.0)
    if body.get("refresh_token"):
        _token_cache["refresh_token"] = body["refresh_token"]
    return _token_cache["token"]


async def _stdio_get_token(ctx: Context) -> str:
    return _get_viya_token()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Tear down warm compute sessions when the server stops."""
    try:
        yield {}
    finally:
        await shutdown_session_cache()


SERVER_VERSION = server_version()
announce_startup("stdio", SERVER_VERSION)
mcp = FastMCP(SERVER_NAME, version=SERVER_VERSION, lifespan=_lifespan)
register_tools(mcp, _stdio_get_token)
register_prompts(mcp)


def main() -> None:
    """Run the MCP server in stdio mode."""
    # No startup banner: FastMCP prints it to stderr, and a driver that does not
    # drain stderr deadlocks on the filled pipe before the first MCP message.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
