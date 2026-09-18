# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for prompt template rendering."""

import pytest
from fastmcp import Client, FastMCP

from sas_mcp_server.prompts import register_prompts


@pytest.fixture
def prompt_mcp():
    mcp = FastMCP("test-prompts")
    register_prompts(mcp)
    return mcp


async def test_all_prompts_registered(prompt_mcp):
    async with Client(prompt_mcp) as client:
        names = {p.name for p in await client.list_prompts()}
    assert names == {"explore_use_case", "score_and_explain"}


async def test_explore_use_case_mentions_the_tools(prompt_mcp):
    async with Client(prompt_mcp) as client:
        result = await client.get_prompt("explore_use_case", {"focus": "readmissions"})
    text = result.messages[0].content.text
    for tool in ("get_use_case", "query_data", "render_chart"):
        assert tool in text
    assert "readmissions" in text


async def test_score_and_explain_carries_record_and_model(prompt_mcp):
    async with Client(prompt_mcp) as client:
        result = await client.get_prompt("score_and_explain", {"record": "age 64, 3 visits", "model": "readmission_gb"})
    text = result.messages[0].content.text
    assert "age 64, 3 visits" in text
    assert "readmission_gb" in text
    for tool in ("describe_model", "score_data"):
        assert tool in text
