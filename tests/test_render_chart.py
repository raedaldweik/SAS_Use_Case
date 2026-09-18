# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the render_chart visualization tool."""

import pytest
from fastmcp import Client

from sas_mcp_server.tools.charts import build_chart_spec


async def test_render_chart_returns_interactive_spec(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        res = await client.call_tool(
            "render_chart",
            {
                "chart_type": "Bar",
                "title": "Patients by Region",
                "data": [{"region": "North", "patients": 120}, {"region": "South", "patients": 140}],
                "x_key": "region",
                "y_keys": ["patients"],
            },
        )
    spec = res.data
    assert spec["kind"] == "chart"
    assert spec["type"] == "bar"  # normalized to lowercase
    assert spec["title"] == "Patients by Region"
    assert spec["xKey"] == "region"
    assert spec["yKeys"] == ["patients"]
    assert spec["stacked"] is False
    assert len(spec["data"]) == 2


async def test_render_chart_accepts_json_encoded_arguments(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        res = await client.call_tool(
            "render_chart",
            {
                "chart_type": "line",
                "title": "x",
                "data": '[{"m": "Jan", "n": 1}]',
                "x_key": "m",
                "y_keys": "n",
            },
        )
    assert res.data["yKeys"] == ["n"]
    assert res.data["data"] == [{"m": "Jan", "n": 1}]


async def test_render_chart_rejects_bad_type(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        with pytest.raises(Exception) as ei:
            await client.call_tool(
                "render_chart",
                {"chart_type": "donut", "title": "x", "data": [{"a": 1}], "x_key": "a", "y_keys": ["a"]},
            )
        assert "chart_type must be one of" in str(ei.value)


async def test_render_chart_rejects_missing_keys(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        with pytest.raises(Exception) as ei:
            await client.call_tool(
                "render_chart",
                {
                    "chart_type": "line",
                    "title": "x",
                    "data": [{"month": "Jan", "sales": 1}],
                    "x_key": "month",
                    "y_keys": ["revenue"],
                },
            )
        assert "not present in the data rows" in str(ei.value)


async def test_render_chart_rejects_empty_data(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        with pytest.raises(Exception) as ei:
            await client.call_tool(
                "render_chart", {"chart_type": "bar", "title": "x", "data": [], "x_key": "a", "y_keys": ["b"]}
            )
        assert "non-empty list" in str(ei.value)


def test_build_chart_spec_caps_rows():
    rows = [{"a": i, "b": i} for i in range(2000)]
    with pytest.raises(ValueError, match="at most"):
        build_chart_spec("bar", "t", rows, "a", ["b"])
