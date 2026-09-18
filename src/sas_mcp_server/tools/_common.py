# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared per-request helpers for the tool modules."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context

from ..viya_client import logger, make_client

# --- tolerant input coercion -------------------------------------------------
# Some MCP clients serialise list/dict parameters as JSON-encoded STRINGS
# ('[{"a": 1}]' instead of the array), and the model cannot fix that from its
# side — pydantic would reject the call before the tool body runs. These
# BeforeValidator coercions absorb that server-side without changing the
# published JSON schema.


def coerce_json_list(value: Any) -> Any:
    """Parse a JSON-encoded string into the list it encodes."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "expected a JSON array (got an unparseable string). Pass the value as a real array."
            ) from exc
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array; the string decodes to a non-array value.")  # noqa: TRY004
        return parsed
    return value


def coerce_str_or_json_list(value: Any) -> Any:
    """Like :func:`coerce_json_list`, but a bare string becomes a 1-element list."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return [value]
            if isinstance(parsed, list):
                return parsed
        return [value]
    return value


def coerce_record_or_records(value: Any) -> Any:
    """Parse a JSON-encoded string into the object or array of objects it encodes."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "expected a JSON object of input values (or an array of them); got an unparseable string."
            ) from exc
        if not isinstance(parsed, dict | list):
            raise ValueError("expected a JSON object (or array of objects) of input values.")  # noqa: TRY004
        return parsed
    return value


def out_of_scope(kind: str, value: str, allowed: list[str]) -> dict[str, Any]:
    """The structured refusal every tool returns for an out-of-scope resource."""
    allowed_str = ", ".join(allowed) if allowed else "(none)"
    return {
        "status": "out_of_scope",
        "message": (
            f"'{value}' is outside this assistant's use case. It is limited to these {kind}: "
            f"{allowed_str}. Call get_use_case to see the full scope."
        ),
    }


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(int(value), hi))


def make_session_helper(get_token: Callable[[Context], Awaitable[str]]):
    """Build the ``viya_session`` context manager bound to *get_token*."""

    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
        """Log tool usage, resolve a Viya token, and yield ``(client, token)``."""
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client, token

    return viya_session
