# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integration tests that call the use-case tools against a real SAS Viya instance.

Requires VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD. Run with:
    uv run python -m pytest -m integration

They target the SAS sample table ``Public.HMEQ`` and skip gracefully when it
(or a required service) is not present. Set ALLOWED_TABLES / ALLOWED_MODELS in
the environment to exercise a scoped configuration instead.
"""

import pytest
from fastmcp import Client

# All integration tests share one session-scoped event loop with the
# session-scoped fixtures (viya_token, integration_mcp_server).
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_TABLE = "Public.HMEQ"


async def test_get_use_case(integration_mcp_server):
    async with Client(integration_mcp_server) as client:
        manifest = (await client.call_tool("get_use_case", {})).data
    assert isinstance(manifest, dict)
    assert "scoped" in manifest
    assert manifest["guidance"]


async def test_describe_and_preview_table(integration_mcp_server):
    async with Client(integration_mcp_server) as client:
        info = (await client.call_tool("describe_table", {"table": _TABLE})).data
        if info.get("status") == "not_found":
            pytest.skip("HMEQ not loaded in Public caslib on this Viya")
        assert info["table"] == _TABLE
        assert info["columns"]
        rows = (await client.call_tool("preview_table", {"table": _TABLE, "limit": 3})).data
        assert rows["columns"] and len(rows["rows"]) <= 3


async def test_query_data_returns_rows(integration_mcp_server):
    async with Client(integration_mcp_server) as client:
        result = (
            await client.call_tool(
                "query_data", {"query": f"select BAD, count(*) as n from {_TABLE} group by BAD", "limit": 10}
            )
        ).data
        if result.get("status") == "table_not_found":
            pytest.skip("HMEQ not loaded in Public caslib on this Viya")
        assert result["columns"] == ["BAD", "n"]
        assert result["rows"]
        # A second query reuses the warm session and still answers.
        again = (await client.call_tool("query_data", {"query": f"select count(*) as n from {_TABLE}"})).data
        assert again["rows"][0]["n"] > 0


async def test_query_data_reports_bad_sql_structurally(integration_mcp_server):
    async with Client(integration_mcp_server) as client:
        result = (await client.call_tool("query_data", {"query": "select nope from Public.DOES_NOT_EXIST"})).data
    assert result["status"] in ("table_not_found", "column_not_found", "query_failed", "schema_not_found")


async def test_render_chart_from_query(integration_mcp_server):
    async with Client(integration_mcp_server) as client:
        result = (
            await client.call_tool("query_data", {"query": f"select JOB, count(*) as n from {_TABLE} group by JOB"})
        ).data
        if result.get("status"):
            pytest.skip("HMEQ not available")
        chart = (
            await client.call_tool(
                "render_chart",
                {"chart_type": "bar", "title": "Loans by job", "data": result["rows"], "x_key": "JOB", "y_keys": ["n"]},
            )
        ).data
    assert chart["kind"] == "chart"


async def test_scoring_workflow(integration_mcp_server):
    async with Client(integration_mcp_server) as client:
        listing = (await client.call_tool("list_models", {})).data
        if not listing["models"]:
            pytest.skip("No MAS modules published — cannot test scoring")
        model = listing["models"][0]["id"]
        sig = (await client.call_tool("describe_model", {"model": model})).data
        assert sig.get("stepId")
        record = {i["name"]: (1 if i["type"] != "string" else "x") for i in sig["inputs"]}
        result = (await client.call_tool("score_data", {"model": model, "input_data": record})).data
        assert result.get("model") == model
        assert "outputs" in result
