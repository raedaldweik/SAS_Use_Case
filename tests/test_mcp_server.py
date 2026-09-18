# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the OAuth HTTP server module."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, Context

from sas_mcp_server import mcp_server
from sas_mcp_server.mcp_server import AuthenticationError
from sas_mcp_server.tools import TOOL_NAMES


def test_authentication_error():
    error = AuthenticationError("Test error message")
    assert error.message == "Test error message"
    assert str(error) == "AuthenticationError: Test error message"


async def test_http_get_token_reads_context_state():
    ctx = MagicMock(spec=Context)
    ctx.get_state = AsyncMock(return_value="viya-token")
    assert await mcp_server._http_get_token(ctx) == "viya-token"
    ctx.get_state.assert_awaited_once_with("access_token")


async def test_http_get_token_requires_a_token():
    ctx = MagicMock(spec=Context)
    ctx.get_state = AsyncMock(return_value=None)
    with pytest.raises(AuthenticationError):
        await mcp_server._http_get_token(ctx)


async def test_http_server_exposes_the_use_case_tools_and_prompts():
    async with Client(mcp_server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
        prompts = {p.name for p in await client.list_prompts()}
    assert names == set(TOOL_NAMES)
    assert prompts == {"explore_use_case", "score_and_explain"}


def test_server_announces_its_version():
    assert mcp_server.SERVER_VERSION
    assert mcp_server.mcp.version == mcp_server.SERVER_VERSION
