# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generic SAS Viya REST helpers.

These functions wrap the common request shapes used by the MCP tools (GET a
JSON document, GET a paginated collection, POST JSON, DELETE a resource) and
build the authenticated :class:`httpx.AsyncClient`. The shared :data:`logger`
lives here as the lowest-level module (above only :mod:`sas_mcp_server.config`)
so every other module can import it without creating an import cycle.
"""

import json
from typing import Any

import fastmcp
import httpx
from fastmcp.utilities.logging import get_logger

from .config import SSL_VERIFY, VIYA_ENDPOINT

logger = get_logger(__name__)


def announce_startup(transport: str, version: str | None) -> str:
    """Log, and return, the first line of the server log: what is running."""
    line = (
        f"sas-mcp-usecase {version or 'unknown'} (fastmcp {fastmcp.__version__}, {transport})"
        f" - connecting to SAS Viya at {VIYA_ENDPOINT}"
    )
    logger.info("%s", line)
    return line


# Viya REST calls can be slow (compute session spin-up, large log fetches);
# give them a generous client timeout.
_CLIENT_TIMEOUT = 300.0

JSONDict = dict[str, Any]

# Cap on how much of a Viya error body is folded into an exception message, so a
# large or HTML error page can't flood the caller's context.
_MAX_ERROR_DETAIL = 800
# How far to follow the nested "errors" chain in a vnd.sas.error+json body.
_MAX_ERROR_DEPTH = 3


def _error_parts(body: JSONDict, depth: int = 0) -> list[str]:
    """Collect the human-readable fields of one vnd.sas.error+json object."""
    parts = [str(body[key]) for key in ("message", "remediation") if body.get(key)]
    details = body.get("details")
    if isinstance(details, list):
        parts.extend(str(item) for item in details if item)
    elif details:
        parts.append(str(details))
    if body.get("errorCode"):
        parts.append(f"(errorCode {body['errorCode']})")
    nested = body.get("errors")
    if isinstance(nested, list) and depth < _MAX_ERROR_DEPTH:
        for item in nested:
            if not isinstance(item, dict):
                continue
            sub = " ".join(_error_parts(item, depth + 1))
            if sub:
                parts.append(f"[{sub}]")
    return parts


def _viya_error_detail(resp: httpx.Response) -> str:
    """Pull the human-readable parts out of a Viya error response body."""
    text = getattr(resp, "text", "")
    if not isinstance(text, str) or not text.strip():
        return ""
    try:
        body = json.loads(text)
    except ValueError:
        return text.strip()[:_MAX_ERROR_DETAIL]
    if not isinstance(body, dict):
        return text.strip()[:_MAX_ERROR_DETAIL]
    return " ".join(_error_parts(body))[:_MAX_ERROR_DETAIL] or text.strip()[:_MAX_ERROR_DETAIL]


def raise_for_viya_status(resp: httpx.Response) -> None:
    """Raise for a 4xx/5xx, quoting the error message Viya itself returned.

    ``httpx.Response.raise_for_status`` reports only the status code, so the
    ``application/vnd.sas.error+json`` body — which names the actual problem —
    is discarded and a model driving these tools sees a bare ``Client error
    '400'`` with nothing to act on. Appending Viya's message gives it
    something to correct. The exception type is unchanged, so existing
    ``except httpx.HTTPStatusError`` handlers keep working.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _viya_error_detail(resp)
        if not detail:
            raise
        request = exc.request
        path = request.url.path or "/"
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code} from {request.method} {path} — Viya reported: {detail}",
            request=request,
            response=exc.response,
        ) from None


async def get_json(
    url: str,
    client: httpx.AsyncClient,
    params: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> JSONDict:
    """GET a JSON response from a Viya REST endpoint."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.get(full_url, headers={"Accept": accept}, params=params or {})
    raise_for_viya_status(resp)
    return resp.json()


async def get_paged_items(
    url: str,
    client: httpx.AsyncClient,
    limit: int = 20,
    start: int = 0,
    filters: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> tuple[list[JSONDict], int]:
    """GET a paginated collection and return the items list plus total count."""
    params: dict[str, Any] = {"start": start, "limit": limit}
    if filters:
        params["filter"] = filters
    if extra_params:
        params.update(extra_params)
    data = await get_json(url, client, params=params, accept="application/vnd.sas.collection+json")
    return data.get("items", []), data.get("count", 0)


async def post_json(
    url: str,
    client: httpx.AsyncClient,
    body: Any | None = None,
    params: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> JSONDict:
    """POST JSON to a Viya REST endpoint and return the response JSON."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.post(
        full_url,
        json=body,
        headers={"Content-Type": "application/json", "Accept": accept},
        params=params or {},
    )
    raise_for_viya_status(resp)
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def delete_resource(url: str, client: httpx.AsyncClient) -> None:
    """DELETE a Viya REST resource."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.delete(full_url)
    raise_for_viya_status(resp)


def make_client(token: str | None) -> httpx.AsyncClient:
    """Create an :class:`httpx.AsyncClient` with auth headers for Viya API calls."""
    headers: dict[str, str] = {}
    if token:
        if not token.startswith("Bearer "):
            token = f"Bearer {token}"
        headers["Authorization"] = token
    return httpx.AsyncClient(headers=headers, verify=SSL_VERIFY, timeout=_CLIENT_TIMEOUT)


def return_items(items: list[JSONDict], prop_selection: list[str]) -> list[dict[str, Any]]:
    """Project each item onto *prop_selection* (missing properties become ``""``)."""
    return [{prop: item.get(prop, "") for prop in prop_selection} for item in items]


def filter_literal(value: str | None) -> str:
    """Escape *value* for use inside a Viya filter string literal."""
    return (value or "").replace("'", "''")


def contains_filter(value: str | None, field: str = "name") -> str | None:
    """Build a Viya ``contains(field,'value')`` substring filter, or ``None``."""
    if not value:
        return None
    return f"contains({field},'{filter_literal(value)}')"
