# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Server settings, read once from the environment (and ``.env``)."""

import os

from dotenv import load_dotenv
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier

from .env import env_bool, env_int
from .exceptions import ConfigError
from .ssl_patch import disable_tls_verification

load_dotenv()

SSL_VERIFY = env_bool("SSL_VERIFY", True)

if not SSL_VERIFY:
    # Self-signed Viya certificates. Patches httpx AND httpx2 (FastMCP 4's
    # client) so the exemption also covers FastMCP's own JWKS/OAuth calls.
    disable_tls_verification()

VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
# Optional OAuth client secret. Leave empty for a public client (PKCE /
# allowpublic). Set it only when you registered the client as confidential.
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
HOST_PORT = int(os.getenv("HOST_PORT", "8134"))
MCP_SIGNING_KEY = os.getenv("MCP_SIGNING_KEY", "default")
CONTEXT_NAME = os.getenv("COMPUTE_CONTEXT_NAME", "SAS Job Execution compute context")
# Name advertised to MCP clients as serverInfo.name during the handshake.
DEFAULT_SERVER_NAME = "SAS Use-Case MCP Server"
SERVER_NAME = os.getenv("MCP_SERVER_NAME", DEFAULT_SERVER_NAME)
# Optional: run every query in one fixed, externally managed compute session
# instead of the per-user pool. Rarely needed; see viya_utils.
COMPUTE_SESSION_ID = os.getenv("COMPUTE_SESSION_ID", "").strip()
# Safety net: the longest a single compute job may run before it is abandoned
# and its session discarded, so a wedged query cannot block the agent forever.
# Ten minutes is far beyond any interactive query; raise it for heavy data.
JOB_POLL_TIMEOUT = float(os.getenv("JOB_POLL_TIMEOUT", "600"))
# Upper bound on the rows one render_chart call accepts.
MAX_CHART_ROWS = env_int("MAX_CHART_ROWS", 1000)
# Upper bound on the records one score_data call scores.
MAX_SCORE_RECORDS = env_int("MAX_SCORE_RECORDS", 100)

_mcp_base_url = os.getenv("MCP_BASE_URL", "").strip()
# An empty value is the documented .env.sample default. Also ignore values
# that are plainly placeholder comments instead of URLs.
MCP_BASE_URL = _mcp_base_url if _mcp_base_url and not _mcp_base_url.startswith("#") else f"http://localhost:{HOST_PORT}"

if not VIYA_ENDPOINT:
    raise ConfigError("VIYA_ENDPOINT is not set. Please set it in the environment variables.")

AUTHORIZATION_ENDPOINT = f"{VIYA_ENDPOINT}/SASLogon/oauth/authorize"
TOKEN_ENDPOINT = f"{VIYA_ENDPOINT}/SASLogon/oauth/token"
JWKS_URI = f"{VIYA_ENDPOINT}/SASLogon/token_keys"


token_verifier = JWTVerifier(jwks_uri=JWKS_URI, audience=[])

viya_auth = OAuthProxy(
    upstream_authorization_endpoint=AUTHORIZATION_ENDPOINT,
    upstream_token_endpoint=TOKEN_ENDPOINT,
    upstream_client_id=CLIENT_ID,
    upstream_client_secret=CLIENT_SECRET or None,
    # A public client (the default registration, no secret) must present no
    # password at all: FastMCP 4 defaults to client_secret_basic and SAS Logon
    # then rejects the exchange with "invalid_client: Missing credentials".
    token_endpoint_auth_method="client_secret_basic" if CLIENT_SECRET else "none",
    jwt_signing_key=MCP_SIGNING_KEY,
    base_url=MCP_BASE_URL,
    # MCP clients open a fresh loopback port per sign-in; FastMCP 4's CIMD
    # path cannot allow that, the dynamic-registration path can.
    enable_cimd=False,
    forward_pkce=True,
    token_verifier=token_verifier,
    valid_scopes=["openid"],
)
