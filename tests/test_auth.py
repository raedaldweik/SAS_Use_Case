# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared grant-selection logic used by the headless servers."""

from sas_mcp_server.auth import select_grant, client_request


def test_refresh_token_wins_over_password():
    data = select_grant(refresh_token="rt", username="u", password="p")
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt"}


def test_refresh_token_only():
    data = select_grant(refresh_token="rt")
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt"}


def test_password_grant_when_no_refresh_token():
    data = select_grant(username="u", password="p")
    assert data == {"grant_type": "password", "username": "u", "password": "p"}


def test_none_when_password_incomplete():
    assert select_grant(username="u") is None
    assert select_grant(password="p") is None


def test_none_when_nothing_configured():
    assert select_grant() is None


def test_client_request_public_sends_client_id_no_basic_auth():
    grant = {"grant_type": "refresh_token", "refresh_token": "rt"}
    data, auth = client_request(grant, "sas-mcp")
    assert data["client_id"] == "sas-mcp"
    assert data["grant_type"] == "refresh_token"
    assert auth is None  # public client: no Basic auth header


def test_client_request_confidential_uses_basic_auth():
    grant = {"grant_type": "refresh_token", "refresh_token": "rt"}
    data, auth = client_request(grant, "sas-mcp", "shh")
    assert data["client_id"] == "sas-mcp"
    assert auth == ("sas-mcp", "shh")


def test_client_request_does_not_mutate_input():
    grant = {"grant_type": "password"}
    client_request(grant, "sas-mcp")
    assert "client_id" not in grant  # original dict untouched
