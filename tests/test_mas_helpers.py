# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the MAS scoring helpers — pure logic plus mocked lookups."""

from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import _make_404_response, _make_mock_response
from sas_mcp_server.helpers import mas_helpers
from sas_mcp_server.helpers.mas_helpers import (
    build_inputs,
    coerce_value,
    fetch_steps,
    pick_step,
    resolve_module,
    shape_outputs,
    signature,
)

STEP = {
    "id": "score",
    "inputs": [
        {"name": "AGE", "type": "decimal"},
        {"name": "GENDER", "type": "string"},
        {"name": "N", "type": "integer"},
    ],
    "outputs": [{"name": "P_1", "type": "decimal"}],
}


def test_pick_step_prefers_score_then_execute_then_first():
    assert pick_step([{"id": "execute"}, {"id": "score"}])["id"] == "score"
    assert pick_step([{"id": "other"}, {"id": "execute"}])["id"] == "execute"
    assert pick_step([{"id": "other"}, {"id": "another"}])["id"] == "other"
    assert pick_step([]) is None


def test_pick_step_honours_explicit_id_case_insensitively():
    assert pick_step([{"id": "score"}, {"id": "execute"}], "EXECUTE")["id"] == "execute"
    assert pick_step([{"id": "score"}], "nope") is None


@pytest.mark.parametrize(
    ("value", "type_name", "expected"),
    [
        ("35", "decimal", 35.0),
        (35, "decimal", 35.0),
        ("2.9", "integer", 2),
        (7, "string", "7"),
        ("", "decimal", None),
        (None, "string", None),
        ("abc", "decimal", "abc"),  # unparseable: left for MAS to report
        ([1, 2], "decimalArray", [1, 2]),
    ],
)
def test_coerce_value(value, type_name, expected):
    assert coerce_value(value, type_name) == expected


def test_build_inputs_matches_case_insensitively_and_reports_extras():
    mapped = build_inputs({"age": "40", "Gender": "F", "zip": "12345"}, STEP)
    assert mapped["inputs"] == [{"name": "AGE", "value": 40.0}, {"name": "GENDER", "value": "F"}]
    assert mapped["used"] == {"AGE": 40.0, "GENDER": "F"}
    assert mapped["ignored"] == ["zip"]
    assert mapped["missing"] == ["N"]


def test_build_inputs_passes_everything_when_step_declares_nothing():
    mapped = build_inputs({"a": 1, "b": "x"}, {"id": "execute"})
    assert mapped["inputs"] == [{"name": "a", "value": 1}, {"name": "b", "value": "x"}]
    assert mapped["ignored"] == [] and mapped["missing"] == []


def test_signature_and_outputs():
    sig = signature(STEP)
    assert sig["stepId"] == "score"
    assert sig["inputs"][0] == {"name": "AGE", "type": "decimal"}
    assert sig["outputs"] == [{"name": "P_1", "type": "decimal"}]
    assert shape_outputs({"outputs": [{"name": "P_1", "value": 0.2}, {"bogus": 1}]}) == {"P_1": 0.2}


def _client(router):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = router
    return client


async def test_resolve_module_by_id_then_by_name_and_caches():
    calls = []

    def route(url, **kwargs):
        calls.append(url)
        if url.endswith("/microanalyticScore/modules/my_model"):
            return _make_mock_response({"id": "my_model", "name": "My Model"})
        if url.endswith("/microanalyticScore/modules/my model"):
            return _make_404_response(url)
        if url.endswith("/microanalyticScore/modules"):
            return _make_mock_response({"items": [{"id": "my_model", "name": "My Model"}], "count": 1})
        raise AssertionError(url)

    client = _client(route)
    assert (await resolve_module(client, "My_Model"))["id"] == "my_model"
    assert (await resolve_module(client, "my model"))["id"] == "my_model"
    n = len(calls)
    assert (await resolve_module(client, "MY MODEL"))["id"] == "my_model"  # cached now
    assert len(calls) == n
    assert await resolve_module(client, "") is None


async def test_resolve_module_unknown_returns_none():
    def route(url, **kwargs):
        if url.endswith("/microanalyticScore/modules"):
            return _make_mock_response({"items": [], "count": 0})
        return _make_404_response(url)

    assert await resolve_module(_client(route), "ghost") is None


async def test_fetch_steps_is_cached():
    calls = []

    def route(url, **kwargs):
        calls.append(url)
        return _make_mock_response({"items": [STEP], "count": 1})

    client = _client(route)
    assert (await fetch_steps(client, "m"))[0]["id"] == "score"
    await fetch_steps(client, "m")
    assert len(calls) == 1
    mas_helpers.clear_cache()
    await fetch_steps(client, "m")
    assert len(calls) == 2
