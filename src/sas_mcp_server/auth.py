# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Shared helpers for the headless MCP servers (stdio and direct-HTTP) to obtain
a SAS Viya OAuth access token on their own behalf.

Both headless servers authenticate to SAS Viya without a per-request browser
flow. The grant they can use depends on how the Viya identity is backed:

* **password grant** works only for identities SAS Logon can authenticate
  directly — e.g. the local ``sasboot`` account or LDAP-backed users.
* **refresh_token grant** works for *any* identity, including ones federated
  through an external provider such as Okta (SAML/OIDC). For federated users
  SAS Logon never sees a password, so the password grant cannot be used; a
  refresh token (obtained once via an interactive login — see
  ``examples/get_refresh_token.py``) is the supported headless path.

``select_grant`` centralises this decision so stdio and direct-HTTP behave
identically: a refresh token, when present, always wins over a username and
password.
"""


def select_grant(refresh_token: str = "", username: str = "",
                 password: str = "") -> dict | None:
    """Return the OAuth token-request form data for the best available grant.

    Preference order:

    1. ``refresh_token`` grant — works for federated (SSO) identities and
       needs no stored password.
    2. ``password`` grant — only for identities SAS Logon authenticates
       directly (e.g. ``sasboot`` or LDAP users).

    Returns ``None`` when no usable credentials are configured, so callers can
    raise a clear authentication error.
    """
    if refresh_token:
        return {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if username and password:
        return {
            "grant_type": "password",
            "username": username,
            "password": password,
        }
    return None


def client_request(grant_data: dict, client_id: str,
                   client_secret: str = "") -> tuple[dict, tuple | None]:
    """Return ``(data, auth)`` for a SAS Logon ``/oauth/token`` request.

    Public clients (registered ``allowpublic``/PKCE, no secret) must send
    ``client_id`` in the request **body** with **no** HTTP Basic auth header —
    SAS Logon rejects an empty-secret Basic header with
    ``invalid_client`` / "Missing credentials". Confidential clients (a secret
    is configured) authenticate with HTTP Basic auth.
    """
    data = {**grant_data, "client_id": client_id}
    auth = (client_id, client_secret) if client_secret else None
    return data, auth
