# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for compute-session and job orchestration (viya_utils)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from conftest import _make_404_response, _make_mock_response
from sas_mcp_server import viya_utils
from sas_mcp_server.viya_utils import (
    _token_user_key,
    get_cached_session,
    get_context_id,
    locked_session,
    reset_cached_session,
    run_job,
    session_key,
    shutdown_session_cache,
    submit_job,
    wait_job,
)


def _client(get=None, post=None):
    client = AsyncMock(spec=httpx.AsyncClient)
    if get is not None:
        client.get.side_effect = get
    if post is not None:
        client.post.return_value = post
    client.delete.return_value = _make_mock_response(status_code=204)
    return client


def _compute_router(session_alive=True, extra=None):
    """A GET router for the context lookup, session state and job endpoints."""
    extra = extra or []

    def route(url, **kwargs):
        for needle, resp in extra:
            if needle in url:
                return resp
        if url.endswith("/compute/contexts"):
            return _make_mock_response({"items": [{"id": "ctx-1"}]})
        if url.endswith("/state") and "/jobs/" not in url:
            return _make_mock_response({}, status_code=200 if session_alive else 404)
        return _make_mock_response({"items": [], "count": 0})

    return route


# --- context / session / job primitives --------------------------------------


async def test_get_context_id_success(mock_env_vars):
    client = _client(get=_compute_router())
    assert await get_context_id(client, "Test Context") == "ctx-1"
    assert client.get.call_args[1]["params"] == {"name": "Test Context"}


async def test_get_context_id_not_found(mock_env_vars):
    client = _client(get=lambda url, **kw: _make_mock_response({"items": []}))
    with pytest.raises(RuntimeError, match="Compute context not found"):
        await get_context_id(client, "Missing")


async def test_submit_job_reports_viya_error_instead_of_bare_id(mock_env_vars):
    request = httpx.Request("POST", "https://test.viya.com/compute/sessions/s/jobs")
    response = httpx.Response(403, request=request)
    resp = _make_mock_response({}, status_code=403, text='{"message": "The session is not available."}')
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("403", request=request, response=response))
    client = _client(post=resp)
    with pytest.raises(httpx.HTTPStatusError, match="The session is not available"):
        await submit_job(client, "s", "data x; run;")


async def test_wait_job_completed_reads_all_pages(mock_env_vars):
    def route(url, **kwargs):
        if url.endswith("/state"):
            return _make_mock_response({}, text="completed")
        if url.endswith("/log"):
            start = kwargs["params"]["start"]
            if start == 0:
                return _make_mock_response({"items": [{"line": f"L{i}"} for i in range(1000)]})
            return _make_mock_response({"items": [{"line": "LAST"}]})
        if url.endswith("/listing"):
            return _make_mock_response({"items": [{"line": "Obs x"}]})
        raise AssertionError(url)

    client = _client(get=route)
    state, log, listing = await wait_job(client, "s", "j", poll=0)
    assert state == "completed"
    assert log.startswith("L0\n") and log.endswith("\nLAST")
    assert listing == "Obs x"


async def test_wait_job_error_state(mock_env_vars):
    def route(url, **kwargs):
        if url.endswith("/state"):
            return _make_mock_response({}, text="error")
        return _make_mock_response({"items": [{"line": "ERROR: boom"}]})

    state, log, _ = await wait_job(_client(get=route), "s", "j", poll=0)
    assert state == "error"
    assert "ERROR: boom" in log


async def test_wait_job_polls_until_terminal(mock_env_vars):
    states = iter(["running", "running", "completed"])

    def route(url, **kwargs):
        if url.endswith("/state"):
            return _make_mock_response({}, text=next(states))
        return _make_mock_response({"items": []})

    with patch("sas_mcp_server.viya_utils.asyncio.sleep", new=AsyncMock()) as sleep:
        state, _, listing = await wait_job(_client(get=route), "s", "j", poll=1)
    assert state == "completed"
    assert listing == "(no listing output)"
    assert sleep.await_count == 2


async def test_wait_job_times_out(mock_env_vars):
    client = _client(get=lambda url, **kw: _make_mock_response({}, text="running"))
    with pytest.raises(TimeoutError, match="did not finish"):
        await wait_job(client, "s", "j", poll=0, timeout=-1)


async def test_wait_job_stops_when_the_session_dies(mock_env_vars):
    client = _client(get=lambda url, **kw: _make_404_response(url))
    with pytest.raises(httpx.HTTPStatusError):
        await wait_job(client, "s", "j", poll=0)


async def test_run_job_submits_and_waits(mock_env_vars):
    def route(url, **kwargs):
        if url.endswith("/state"):
            return _make_mock_response({}, text="completed")
        return _make_mock_response({"items": [{"line": "ok"}]})

    client = _client(get=route, post=_make_mock_response({"id": "J1"}, status_code=201))
    state, log, listing = await run_job(client, "sess", "proc sql; quit;", poll=0)
    assert state == "completed"
    assert client.post.call_args[0][0].endswith("/compute/sessions/sess/jobs")
    assert client.post.call_args[1]["json"] == {"code": ["proc sql; quit;"]}


# --- per-user keys -----------------------------------------------------------


def _jwt(sub):
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


def test_token_user_key_uses_jwt_sub():
    assert _token_user_key(_jwt("alice")) == "sub:alice"
    assert _token_user_key("Bearer " + _jwt("alice")) == "sub:alice"


def test_token_user_key_falls_back_on_undecodable_token():
    assert _token_user_key("opaque").startswith("token:")
    assert _token_user_key("opaque") == _token_user_key("opaque")
    assert _token_user_key("opaque") != _token_user_key("other")


def test_session_key_is_constant_in_fixed_session_mode():
    with patch.object(viya_utils, "COMPUTE_SESSION_ID", "0001"):
        assert session_key(_jwt("a"), "c") == session_key(_jwt("b"), "d") == ("fixed", "0001")


# --- the session pool --------------------------------------------------------


async def test_get_cached_session_creates_then_reuses(mock_env_vars):
    client = _client(get=_compute_router(), post=_make_mock_response({"id": "sess-1"}, status_code=201))
    first = await get_cached_session(client, "ctx", _jwt("alice"))
    second = await get_cached_session(client, "ctx", _jwt("alice"))
    assert first == second == "sess-1"
    assert client.post.call_count == 1


async def test_get_cached_session_recreates_when_reaped(mock_env_vars):
    alive = {"value": True}

    def route(url, **kwargs):
        if url.endswith("/state") and "/jobs/" not in url:
            return _make_mock_response({}, status_code=200 if alive["value"] else 404)
        return _compute_router()(url, **kwargs)

    posts = iter([_make_mock_response({"id": "sess-1"}, 201), _make_mock_response({"id": "sess-2"}, 201)])
    client = _client(get=route)
    client.post.side_effect = lambda *a, **k: next(posts)
    assert await get_cached_session(client, "ctx", _jwt("a")) == "sess-1"
    alive["value"] = False
    assert await get_cached_session(client, "ctx", _jwt("a")) == "sess-2"


async def test_get_cached_session_is_per_user(mock_env_vars):
    posts = iter([_make_mock_response({"id": "sess-a"}, 201), _make_mock_response({"id": "sess-b"}, 201)])
    client = _client(get=_compute_router())
    client.post.side_effect = lambda *a, **k: next(posts)
    assert await get_cached_session(client, "ctx", _jwt("alice")) == "sess-a"
    assert await get_cached_session(client, "ctx", _jwt("bob")) == "sess-b"


async def test_reset_cached_session_deletes_and_forgets(mock_env_vars):
    client = _client(get=_compute_router(), post=_make_mock_response({"id": "sess-1"}, 201))
    await get_cached_session(client, "ctx", _jwt("a"))
    assert await reset_cached_session(client, "ctx", _jwt("a")) == "sess-1"
    assert client.delete.call_args[0][0].endswith("/compute/sessions/sess-1")
    assert await reset_cached_session(client, "ctx", _jwt("a")) is None


async def test_shutdown_deletes_every_cached_session(mock_env_vars):
    client = _client(get=_compute_router(), post=_make_mock_response({"id": "sess-1"}, 201))
    await get_cached_session(client, "ctx", _jwt("a"))
    deleter = AsyncMock(spec=httpx.AsyncClient)
    deleter.__aenter__ = AsyncMock(return_value=deleter)
    deleter.__aexit__ = AsyncMock(return_value=False)
    with patch("sas_mcp_server.viya_utils.make_client", return_value=deleter):
        await shutdown_session_cache()
    deleter.delete.assert_awaited_once()
    assert deleter.delete.call_args[0][0].endswith("/compute/sessions/sess-1")


# --- locked_session ------------------------------------------------------------


async def test_locked_session_serialises_jobs_in_one_session(mock_env_vars):
    """Two concurrent callers sharing a token must not interleave inside the session."""
    client = _client(get=_compute_router(), post=_make_mock_response({"id": "sess-1"}, 201))
    token = _jwt("service-account")
    events = []

    async def worker(name):
        async with locked_session(client, token) as sid:
            events.append(f"{name}-in:{sid}")
            await asyncio.sleep(0.01)
            events.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))
    assert events == ["a-in:sess-1", "a-out", "b-in:sess-1", "b-out"]


async def test_locked_session_abandons_the_session_on_timeout(mock_env_vars):
    client = _client(get=_compute_router(), post=_make_mock_response({"id": "sess-1"}, 201))
    token = _jwt("a")
    with pytest.raises(TimeoutError):
        async with locked_session(client, token):
            raise TimeoutError("job overran")
    client.delete.assert_awaited_once()
    assert client.delete.call_args[0][0].endswith("/compute/sessions/sess-1")
    # The next caller gets a fresh session rather than queueing behind the wedged one.
    client.post.return_value = _make_mock_response({"id": "sess-2"}, 201)
    async with locked_session(client, token) as sid:
        assert sid == "sess-2"


async def test_locked_session_fixed_mode_skips_lookup(mock_env_vars):
    client = _client(get=_compute_router())
    with patch.object(viya_utils, "COMPUTE_SESSION_ID", "0001"):
        async with locked_session(client, "tok") as sid:
            assert sid == "0001"
    client.get.assert_not_called()
    client.post.assert_not_called()
