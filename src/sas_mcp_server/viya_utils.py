# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Viya compute session and job orchestration.

These helpers drive the Compute service end to end: resolve a compute context,
open a session, submit code, and poll for completion.

To avoid paying the (slow) session spin-up cost on every query, compute
sessions are pooled by :class:`_ComputeSessionCache`: one reusable session per
authenticated user and compute context. In the headless modes every request
carries the same service-account token, so many chat users share one warm
session; a per-session **job lock** serialises their jobs, because a compute
session runs one job at a time. Queries here take about a second, so waiting
on the lock is cheaper than spinning up a session per call.

A job that overruns ``JOB_POLL_TIMEOUT`` is abandoned and its session
discarded, so a wedged query can never block every later call.
"""

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from .config import COMPUTE_SESSION_ID, CONTEXT_NAME, JOB_POLL_TIMEOUT, VIYA_ENDPOINT
from .viya_client import logger, make_client, raise_for_viya_status


async def get_context_id(client: httpx.AsyncClient, context_name: str) -> str:
    """Return the id of the named compute context, raising if it is absent."""
    url = f"{VIYA_ENDPOINT}/compute/contexts"
    resp = await client.get(url, params={"name": context_name})
    raise_for_viya_status(resp)
    coll = resp.json()
    items = coll.get("items", [])
    if not items:
        raise RuntimeError(f"Compute context not found: {context_name}")
    return items[0]["id"]


async def create_session(client: httpx.AsyncClient, context_id: str, name: str = "sas-mcp-usecase") -> str:
    """Create a compute session in *context_id* and return its id."""
    url = f"{VIYA_ENDPOINT}/compute/contexts/{context_id}/sessions"
    resp = await client.post(url, json={"name": name})
    raise_for_viya_status(resp)
    return resp.json()["id"]


async def delete_session(client: httpx.AsyncClient, sid: str) -> None:
    """Delete compute session *sid* (raises on failure)."""
    try:
        await client.delete(f"{VIYA_ENDPOINT}/compute/sessions/{sid}")
        logger.info("Session %s deleted successfully", sid)
    except Exception:
        logger.exception("Failed to delete session %s", sid)
        raise


def _token_user_key(token: str) -> str:
    """Derive a stable per-user cache key from a Viya access token.

    Viya access tokens are JWTs; the ``sub`` claim (or another identity claim)
    is read *without* verifying the signature — the auth layer already did. A
    token that is not a decodable JWT falls back to a hash of the string.
    """
    raw = token[7:] if token.startswith("Bearer ") else token
    parts = raw.split(".")
    if len(parts) >= 2:
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            for claim in ("sub", "uid", "user_name", "user_id"):
                value = payload.get(claim)
                if value:
                    return f"{claim}:{value}"
        except (binascii.Error, ValueError, json.JSONDecodeError):
            pass
    return "token:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def session_key(token: str, context_name: str) -> tuple[str, str]:
    """The cache key for the token's user and compute context.

    In fixed-session mode (``COMPUTE_SESSION_ID``) every caller shares the one
    session, so the key is constant and the job lock covers them all.
    """
    if COMPUTE_SESSION_ID:
        return ("fixed", COMPUTE_SESSION_ID)
    return (_token_user_key(token), context_name)


async def _session_is_alive(client: httpx.AsyncClient, session_id: str) -> bool:
    """Return ``True`` if *session_id* still exists server-side."""
    try:
        resp = await client.get(f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/state")
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


class _ComputeSessionCache:
    """Process-wide pool of reusable compute sessions, keyed by (user, context)."""

    SESSION_NAME = "sas-mcp-usecase"

    def __init__(self) -> None:
        # key -> (session_id, most recent token seen). The token is kept so
        # shutdown can authenticate the delete calls.
        self._sessions: dict[tuple[str, str], tuple[str, str]] = {}
        # Serialises create/reset of one key without blocking the others.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Serialises the jobs run in one session: a compute session executes
        # one job at a time, and concurrent chat users share a session in the
        # headless modes.
        self._job_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: tuple[str, str], table: dict[tuple[str, str], asyncio.Lock]) -> asyncio.Lock:
        async with self._guard:
            lock = table.get(key)
            if lock is None:
                lock = asyncio.Lock()
                table[key] = lock
            return lock

    async def job_lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        return await self._lock_for(key, self._job_locks)

    async def get_or_create(
        self, client: httpx.AsyncClient, context_name: str, key: tuple[str, str], token: str
    ) -> str:
        """Return a live session id for *key*, creating one when needed."""
        if COMPUTE_SESSION_ID:
            return COMPUTE_SESSION_ID
        lock = await self._lock_for(key, self._locks)
        async with lock:
            cached = self._sessions.get(key)
            if cached is not None:
                sid, _ = cached
                if await _session_is_alive(client, sid):
                    logger.info("Reusing cached compute session %s", sid)
                    self._sessions[key] = (sid, token)
                    return sid
                logger.info("Cached compute session %s is gone; recreating", sid)
                self._sessions.pop(key, None)
            context_id = await get_context_id(client, context_name)
            sid = await create_session(client, context_id, name=self.SESSION_NAME)
            self._sessions[key] = (sid, token)
            logger.info("Created and cached compute session %s", sid)
            return sid

    async def reset(self, client: httpx.AsyncClient, key: tuple[str, str]) -> str | None:
        """Drop the cached session for *key* and delete it server-side."""
        if COMPUTE_SESSION_ID:
            return None
        lock = await self._lock_for(key, self._locks)
        async with lock:
            cached = self._sessions.pop(key, None)
        if cached is None:
            return None
        sid, _ = cached
        await delete_session(client, sid)
        return sid

    async def abandon(self, client: httpx.AsyncClient, key: tuple[str, str]) -> None:
        """Forget the session for *key* and delete it best-effort (a wedged job)."""
        if COMPUTE_SESSION_ID:
            logger.warning("Job overran in fixed session %s; it is externally managed", COMPUTE_SESSION_ID)
            return
        lock = await self._lock_for(key, self._locks)
        async with lock:
            cached = self._sessions.pop(key, None)
        if cached is None:
            return
        sid, _ = cached
        try:
            await delete_session(client, sid)
        except Exception:
            logger.warning("Could not delete abandoned compute session %s", sid, exc_info=True)

    async def shutdown(self) -> None:
        """Delete every cached compute session server-side (best effort)."""
        async with self._guard:
            entries = list(self._sessions.values())
            self._sessions.clear()
            self._locks.clear()
            self._job_locks.clear()
        if not entries:
            return
        logger.info("Deleting %d cached compute session(s) on shutdown", len(entries))
        for sid, token in entries:
            try:
                async with make_client(token) as client:
                    await delete_session(client, sid)
            except Exception:
                logger.warning("Could not delete compute session %s on shutdown", sid, exc_info=True)

    def clear(self) -> None:
        """Forget all cached sessions without deleting them (test isolation)."""
        self._sessions.clear()
        self._locks.clear()
        self._job_locks.clear()


_SESSION_CACHE = _ComputeSessionCache()


async def get_cached_session(client: httpx.AsyncClient, context_name: str, token: str) -> str:
    """Return the reusable compute session id for the token's user + context."""
    return await _SESSION_CACHE.get_or_create(client, context_name, session_key(token, context_name), token)


async def reset_cached_session(client: httpx.AsyncClient, context_name: str, token: str) -> str | None:
    """Delete and forget the cached compute session for the token's user."""
    return await _SESSION_CACHE.reset(client, session_key(token, context_name))


async def shutdown_session_cache() -> None:
    """Delete all cached compute sessions server-side (call from server lifespan)."""
    await _SESSION_CACHE.shutdown()


def clear_session_cache() -> None:
    """Forget all cached compute sessions (test isolation helper)."""
    _SESSION_CACHE.clear()


@asynccontextmanager
async def locked_session(client: httpx.AsyncClient, token: str, context_name: str = CONTEXT_NAME) -> AsyncIterator[str]:
    """Yield the caller's warm compute session id while holding its job lock.

    Everything inside the block — submitting the job, reading its result
    tables back — runs without another caller's job interleaving. A job that
    overruns ``JOB_POLL_TIMEOUT`` raises :class:`TimeoutError`; the session is
    then abandoned so the next call starts clean instead of queueing behind
    the wedged job.
    """
    key = session_key(token, context_name)
    sid = await _SESSION_CACHE.get_or_create(client, context_name, key, token)
    lock = await _SESSION_CACHE.job_lock_for(key)
    async with lock:
        try:
            yield sid
        except TimeoutError:
            await _SESSION_CACHE.abandon(client, key)
            raise


async def submit_job(client: httpx.AsyncClient, session_id: str, code: str) -> str:
    """Submit *code* as a job in *session_id* and return the job id."""
    body = {"code": code.splitlines()}
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs"
    resp = await client.post(url, json=body)
    # Unchecked, a refused submit is parsed as though it were a job and the
    # failure reaches the caller as the single word 'id'.
    raise_for_viya_status(resp)
    return resp.json()["id"]


# Page size for compute log/listing collection fetches, and a loop backstop far
# above any real log. Hitting the backstop appends an explicit marker.
_LINES_PAGE_LIMIT = 1000
_LINES_MAX_PAGES = 10_000


async def _fetch_all_lines(client: httpx.AsyncClient, url: str) -> list[str]:
    """Collect every ``line`` of a compute log/listing collection (it is paged)."""
    lines: list[str] = []
    start = 0
    for _ in range(_LINES_MAX_PAGES):
        resp = await client.get(url, params={"start": start, "limit": _LINES_PAGE_LIMIT})
        raise_for_viya_status(resp)
        items = resp.json().get("items", [])
        lines.extend(item.get("line", "") for item in items)
        if len(items) < _LINES_PAGE_LIMIT:
            return lines
        start += _LINES_PAGE_LIMIT
    lines.append(f"[output truncated after {len(lines)} lines]")
    return lines


async def wait_job(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
    poll: float = 2,
    timeout: float | None = None,
) -> tuple[str, str, str]:
    """Poll *job_id* until it reaches a terminal state; return (state, log, listing).

    Raises :class:`TimeoutError` after *timeout* seconds (default
    ``JOB_POLL_TIMEOUT``) so a wedged job cannot hang the caller forever.
    """
    timeout = JOB_POLL_TIMEOUT if timeout is None else timeout
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/state"
        resp = await client.get(state_url)
        # Read as plain text, so an error body would otherwise be treated as a
        # state name — never a terminal one — and poll forever.
        raise_for_viya_status(resp)
        state = resp.text.strip()
        if state in ("completed", "error", "warning", "canceled"):
            log_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/log"
            log_text = "\n".join(await _fetch_all_lines(client, log_url))
            listing_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/listing"
            listing_lines = await _fetch_all_lines(client, listing_url)
            listing_text = "\n".join(listing_lines) if listing_lines else "(no listing output)"
            return state, log_text, listing_text
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"SAS job {job_id} did not finish within {timeout:.0f}s (last state: '{state}'). "
                f"Narrow the query, or raise JOB_POLL_TIMEOUT if it legitimately runs longer."
            )
        await asyncio.sleep(poll)


async def run_job(client: httpx.AsyncClient, session_id: str, code: str, poll: float = 2) -> tuple[str, str, str]:
    """Submit *code* in *session_id* and wait for it: ``(state, log, listing)``."""
    job_id = await submit_job(client, session_id, code)
    logger.info("Job submitted: %s", job_id)
    state, log_text, listing_text = await wait_job(client, session_id, job_id, poll=poll)
    logger.info("Job completed: %s", state)
    return state, log_text, listing_text
