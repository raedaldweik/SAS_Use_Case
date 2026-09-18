# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pytest configuration and shared fixtures for the use-case MCP server tests."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from dotenv import load_dotenv

# Load .env once, before any sas_mcp_server module is imported, so config
# values captured at import time reflect the developer's local environment.
load_dotenv()

from fastmcp import FastMCP  # noqa: E402  (must follow load_dotenv above)

SCOPE_VARS = ("ALLOWED_TABLES", "ALLOWED_MODELS", "ALLOWED_DECISIONS", "USE_CASE_NAME", "USE_CASE_DESCRIPTION")


@pytest.fixture(autouse=True)
def _isolate_process_caches():
    """Keep the compute-session pool and the MAS lookup cache from leaking between tests."""
    from sas_mcp_server.helpers.mas_helpers import clear_cache
    from sas_mcp_server.viya_utils import clear_session_cache

    clear_session_cache()
    clear_cache()
    yield
    clear_session_cache()
    clear_cache()


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for testing."""
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("HOST_PORT", "8134")
    monkeypatch.setenv("MCP_SIGNING_KEY", "test-key")
    monkeypatch.setenv("COMPUTE_CONTEXT_NAME", "Test Context")


@pytest.fixture
def mock_httpx_client():
    """Mock httpx AsyncClient for testing API calls."""
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
def mock_access_token():
    return "mock-access-token-12345"


# ---------------------------------------------------------------------------
# Mock HTTP plumbing
# ---------------------------------------------------------------------------


def _make_mock_response(json_data=None, status_code=200, text=None):
    """Create a mock httpx response."""
    resp = AsyncMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    resp.content = b"{}" if json_data is not None or status_code != 204 else b""
    resp.text = text or ""
    resp.headers = {"Content-Type": "application/json"}
    return resp


def _make_404_response(url="https://test.viya.com/x"):
    """A response whose raise_for_status raises a 404 HTTPStatusError."""
    request = httpx.Request("GET", url)
    response = httpx.Response(404, request=request)
    resp = _make_mock_response({}, status_code=404)
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("404", request=request, response=response))
    return resp


def _make_mock_client():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    paged_resp = _make_mock_response({"items": [], "count": 0})
    post_resp = _make_mock_response({"id": "test-id"}, status_code=201)
    post_resp.content = b'{"id": "test-id"}'
    delete_resp = _make_mock_response(status_code=204)
    mock_client.get.return_value = paged_resp
    mock_client.post.return_value = post_resp
    mock_client.delete.return_value = delete_resp
    return mock_client


def route_get(mock_client, routes):
    """Route mock GETs by URL substring: ``routes`` is ``[(substring, response), ...]``, first match wins."""
    default = mock_client.get.return_value

    def _route(url, **kwargs):
        for needle, resp in routes:
            if needle in url:
                return resp
        return default

    mock_client.get.side_effect = _route


def stub_compute_session(mock_client):
    """Answer the compute-context lookup and session create a query needs."""
    route_get(mock_client, [("/compute/contexts", _make_mock_response({"items": [{"id": "ctx-id"}]}))])
    mock_client.post.return_value = _make_mock_response({"id": "sess-id"}, status_code=201)


def build_server(monkeypatch, env=None):
    """Register the tools on a fresh server with a mock Viya client.

    ``env`` sets use-case variables for the registration; scope variables not
    in it are removed, so a developer's .env cannot leak in. Returns
    ``(mcp, mock_client, patcher)``; the caller stops the patcher.
    """
    env = env or {}
    for var in SCOPE_VARS:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mock_client = _make_mock_client()
    patcher = patch("sas_mcp_server.tools._common.make_client", return_value=mock_client)
    patcher.start()
    mcp = FastMCP("Test Server")

    async def mock_get_token(ctx):
        return "test-token"

    from sas_mcp_server.tools import register_tools

    register_tools(mcp, mock_get_token)
    return mcp, mock_client, patcher


@pytest.fixture
def mcp_server_with_mock_client(monkeypatch):
    """An unscoped server plus the mock client that captures every HTTP call."""
    mcp, mock_client, patcher = build_server(monkeypatch)
    try:
        yield mcp, mock_client
    finally:
        patcher.stop()


@pytest.fixture
def scoped_server(monkeypatch):
    """Factory: ``scoped_server({"ALLOWED_TABLES": ...})`` → ``(mcp, mock_client)``."""
    patchers = []

    def _factory(env):
        mcp, mock_client, patcher = build_server(monkeypatch, env)
        patchers.append(patcher)
        return mcp, mock_client

    try:
        yield _factory
    finally:
        for patcher in patchers:
            patcher.stop()


# ---------------------------------------------------------------------------
# Integration test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def viya_credentials():
    """Load Viya credentials from environment. Skip if not available."""
    endpoint = os.getenv("VIYA_ENDPOINT", "")
    username = os.getenv("VIYA_USERNAME", "")
    password = os.getenv("VIYA_PASSWORD", "")
    if not all([endpoint, username, password]):
        pytest.skip("VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD required")
    return {"endpoint": endpoint, "username": username, "password": password}


@pytest.fixture(scope="session")
def viya_token(viya_credentials):
    """Get a real Viya access token via password grant."""
    from sas_mcp_server.config import SSL_VERIFY

    client_id = os.getenv("TEST_CLIENT_ID", "sas.cli")
    resp = httpx.post(
        f"{viya_credentials['endpoint']}/SASLogon/oauth/token",
        auth=(client_id, ""),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "password",
            "username": viya_credentials["username"],
            "password": viya_credentials["password"],
        },
        verify=SSL_VERIFY,
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def integration_mcp_server(viya_token):
    """An MCP server with real Viya auth for integration tests."""
    mcp = FastMCP("Integration Test Server")
    _token = viya_token

    async def real_get_token(ctx):
        return _token

    from sas_mcp_server.prompts import register_prompts
    from sas_mcp_server.tools import register_tools

    register_tools(mcp, real_get_token)
    register_prompts(mcp)
    return mcp
