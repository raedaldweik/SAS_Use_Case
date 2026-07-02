# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the performance features: pooled HTTP clients and compute
session reuse (including the query path with its warm CAS connection)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sas_mcp_server import viya_utils
from sas_mcp_server.viya_utils import (
    run_one_snippet,
    run_query_rows,
    _make_client,
    _query_sas_code,
)


def _mock_client_for(mock_client_class):
    """An AsyncMock client usable on both the pooled and per-call paths."""
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client_class.return_value = mock_client
    return mock_client


def _response(json_value=None, text=None):
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    if json_value is not None:
        resp.json = MagicMock(return_value=json_value)
    if text is not None:
        resp.text = text
    return resp


def _job_run_responses():
    """GET responses for one successful job run: state, log, listing."""
    return [
        _response(text="completed"),
        _response(json_value={"items": [{"line": "Log output"}]}),
        _response(json_value={"items": [{"line": "Listing output"}]}),
    ]


# ---------------------------------------------------------------------------
# Compute session reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_snippet_reuses_pooled_session(sample_sas_code, mock_env_vars):
    """The second call must skip context lookup + session creation and reuse
    the session parked by the first call — the core latency fix."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),  # context (1st only)
            *_job_run_responses(),
            *_job_run_responses(),  # 2nd call: no context lookup
        ]
        mock_client.post.side_effect = [
            _response(json_value={"id": "sess-1"}),  # create session (1st only)
            _response(json_value={"id": "job-1"}),
            _response(json_value={"id": "job-2"}),   # 2nd call: straight to job
        ]

        r1 = await run_one_snippet(sample_sas_code, "1", "tok")
        r2 = await run_one_snippet(sample_sas_code, "2", "tok")

        assert r1[1] == r2[1] == "completed"
        assert mock_client.post.call_count == 3
        mock_client.delete.assert_not_called()
        assert viya_utils._session_pool.get("Bearer tok") == ["sess-1"]


@pytest.mark.asyncio
async def test_run_query_rows_reuses_pooled_session(mock_env_vars):
    """query_table's SQL path must also park and reuse its compute session."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        cols_resp = _response(json_value={"items": [{"name": "SUPPLIER"}]})
        rows_resp = _response(json_value={"items": [{"cells": ["ACME"]}]})

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),  # context
            *_job_run_responses(),
            cols_resp, rows_resp,       # read WORK._MCPQ back
        ]
        mock_client.post.side_effect = [
            _response(json_value={"id": "sess-1"}),
            _response(json_value={"id": "job-1"}),
        ]

        out = await run_query_rows("select SUPPLIER from t", "tok", limit=10)

        assert out["error"] is False
        assert out["rows"] == [{"SUPPLIER": "ACME"}]
        mock_client.delete.assert_not_called()
        assert viya_utils._session_pool.get("Bearer tok") == ["sess-1"]


@pytest.mark.asyncio
async def test_run_query_rows_sas_error_keeps_session(mock_env_vars):
    """A bad SELECT (SAS error) is the agent's problem, not the session's —
    the session must go back to the pool for the corrected retry."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),
            _response(text="error"),
            _response(json_value={"items": [{"line": "ERROR: bad sql"}]}),
            _response(json_value={"items": []}),
        ]
        mock_client.post.side_effect = [
            _response(json_value={"id": "sess-1"}),
            _response(json_value={"id": "job-1"}),
        ]

        out = await run_query_rows("select nonsense", "tok")

        assert out["error"] is True
        assert "bad sql" in out["log"]
        mock_client.delete.assert_not_called()
        assert viya_utils._session_pool.get("Bearer tok") == ["sess-1"]


@pytest.mark.asyncio
async def test_dead_pooled_session_is_replaced_and_job_retried(sample_sas_code, mock_env_vars):
    """A pooled session that died server-side must be discarded and the job
    retried once on a brand-new session."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_client_class:
        mock_client = _mock_client_for(mock_client_class)

        viya_utils._session_pool["Bearer tok"] = ["stale-sess"]

        submit_fail = _response(json_value=None)
        submit_fail.json = MagicMock(side_effect=KeyError("id"))  # dead session

        mock_client.get.side_effect = [
            _response(json_value={"items": [{"id": "ctx-id"}]}),
            *_job_run_responses(),
        ]
        mock_client.post.side_effect = [
            submit_fail,                                # submit on stale session
            _response(json_value={"id": "sess-2"}),     # create fresh session
            _response(json_value={"id": "job-1"}),      # submit succeeds
        ]

        result = await run_one_snippet(sample_sas_code, "1", "tok")

        assert result[1] == "completed"
        mock_client.delete.assert_called_once()
        assert "stale-sess" in str(mock_client.delete.call_args)
        assert viya_utils._session_pool.get("Bearer tok") == ["sess-2"]


# ---------------------------------------------------------------------------
# Reuse-safe CAS connection for the query path
# ---------------------------------------------------------------------------


def test_query_sas_code_guards_cas_connect():
    """The CAS connect must be conditional (sessfound) so a reused compute
    session doesn't fail on 'session name already in use'."""
    code = _query_sas_code("select 1 from t")
    assert "sessfound(_mcpcas)" in code
    assert "cas _mcpcas;" in code
    assert "caslib _all_ assign;" in code
    assert "create table work._mcpq" in code


# ---------------------------------------------------------------------------
# Pooled HTTP clients
# ---------------------------------------------------------------------------


def test_make_client_reuses_pooled_client_per_token(mock_env_vars):
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_cls:
        mock_cls.return_value = MagicMock(is_closed=False)
        lease1 = _make_client("my-token")
        lease2 = _make_client("Bearer my-token")  # same identity, same client
        assert mock_cls.call_count == 1
        assert lease1._client is lease2._client
