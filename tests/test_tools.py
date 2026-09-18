# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the generic Viya REST helpers (viya_client)."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from conftest import _make_mock_response
from sas_mcp_server.viya_client import (
    contains_filter,
    delete_resource,
    get_json,
    get_paged_items,
    make_client,
    post_json,
    raise_for_viya_status,
    return_items,
)


async def test_get_json_success(mock_httpx_client, mock_env_vars):
    mock_httpx_client.get.return_value = _make_mock_response({"id": "x"})
    assert await get_json("/some/path", mock_httpx_client) == {"id": "x"}
    url = mock_httpx_client.get.call_args[0][0]
    assert url.endswith("/some/path")
    assert mock_httpx_client.get.call_args[1]["headers"]["Accept"] == "application/json"


async def test_get_paged_items_sends_paging_and_filter(mock_httpx_client, mock_env_vars):
    mock_httpx_client.get.return_value = _make_mock_response({"items": [{"a": 1}], "count": 7})
    items, count = await get_paged_items("/coll", mock_httpx_client, limit=5, start=10, filters="eq(name,'x')")
    assert items == [{"a": 1}] and count == 7
    params = mock_httpx_client.get.call_args[1]["params"]
    assert params == {"start": 10, "limit": 5, "filter": "eq(name,'x')"}
    assert mock_httpx_client.get.call_args[1]["headers"]["Accept"] == "application/vnd.sas.collection+json"


async def test_post_json_returns_empty_dict_on_204(mock_httpx_client, mock_env_vars):
    mock_httpx_client.post.return_value = _make_mock_response(status_code=204)
    assert await post_json("/x", mock_httpx_client, body={"a": 1}) == {}
    assert mock_httpx_client.post.call_args[1]["json"] == {"a": 1}


async def test_delete_resource(mock_httpx_client, mock_env_vars):
    mock_httpx_client.delete.return_value = _make_mock_response(status_code=204)
    await delete_resource("/x/1", mock_httpx_client)
    assert mock_httpx_client.delete.call_args[0][0].endswith("/x/1")


def test_raise_for_viya_status_quotes_the_viya_message():
    request = httpx.Request("GET", "https://viya/casManagement/servers/x")
    response = httpx.Response(400, request=request)
    resp = MagicMock()
    resp.status_code = 400
    resp.text = '{"message": "The ID contains invalid characters.", "remediation": "Fix the ID.", "errorCode": 12}'
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("400", request=request, response=response))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        raise_for_viya_status(resp)
    msg = str(ei.value)
    assert "HTTP 400 from GET /casManagement/servers/x" in msg
    assert "The ID contains invalid characters." in msg and "Fix the ID." in msg


def test_raise_for_viya_status_passes_success():
    resp = _make_mock_response({"ok": True})
    raise_for_viya_status(resp)  # no raise


def test_make_client_adds_bearer_prefix(mock_env_vars):
    client = make_client("abc")
    assert client.headers["Authorization"] == "Bearer abc"
    client2 = make_client("Bearer xyz")
    assert client2.headers["Authorization"] == "Bearer xyz"
    assert "Authorization" not in make_client(None).headers


def test_return_items_and_contains_filter():
    assert return_items([{"id": 1, "x": 2}], ["id", "name"]) == [{"id": 1, "name": ""}]
    assert contains_filter(None) is None
    assert contains_filter("O'Brien") == "contains(name,'O''Brien')"


async def test_async_mock_client_shape():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _make_mock_response({"a": 1})
    assert (await client.get("u")).json() == {"a": 1}
