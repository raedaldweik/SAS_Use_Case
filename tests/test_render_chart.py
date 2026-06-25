# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the render_chart visualization tool."""
import pytest
from unittest.mock import AsyncMock, patch
from fastmcp import FastMCP, Client


def _build_server():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    patcher = patch("sas_mcp_server.tools._make_client", return_value=mock_client)
    patcher.start()
    mcp = FastMCP("Chart Test Server")

    async def mock_get_token(ctx):
        return "test-token"

    from sas_mcp_server.tools import register_tools
    register_tools(mcp, mock_get_token)
    return mcp, patcher


async def test_render_chart_returns_png_image_and_metadata():
    mcp, patcher = _build_server()
    try:
        async with Client(mcp) as client:
            res = await client.call_tool("render_chart", {
                "chart_type": "Bar",
                "title": "Sales by Month",
                "data": [{"month": "Jan", "sales": 120}, {"month": "Feb", "sales": 140}],
                "x_key": "month",
                "y_keys": ["sales"],
            })
        # Light structured metadata (no row-data echo).
        meta = res.data
        assert meta["kind"] == "chart"
        assert meta["type"] == "bar"            # normalized to lowercase
        assert meta["title"] == "Sales by Month"
        assert meta["points"] == 2
        # An actual PNG image is returned for the host to display.
        image_blocks = [c for c in res.content if getattr(c, "type", None) == "image"]
        assert image_blocks, "expected an image content block"
        assert image_blocks[0].mimeType == "image/png"
        assert image_blocks[0].data  # base64 payload present
    finally:
        patcher.stop()


async def test_render_chart_renders_each_chart_type():
    mcp, patcher = _build_server()
    data = [{"k": "A", "v1": 10, "v2": 5}, {"k": "B", "v1": 7, "v2": 9}]
    try:
        async with Client(mcp) as client:
            for ct in ("bar", "line", "area", "pie", "scatter"):
                res = await client.call_tool("render_chart", {
                    "chart_type": ct, "title": f"{ct} chart",
                    "data": data, "x_key": "k", "y_keys": ["v1"],
                })
                image_blocks = [c for c in res.content
                                if getattr(c, "type", None) == "image"]
                assert image_blocks, f"{ct} produced no image"
    finally:
        patcher.stop()


async def test_render_chart_rejects_bad_type():
    mcp, patcher = _build_server()
    try:
        async with Client(mcp) as client:
            with pytest.raises(Exception) as ei:
                await client.call_tool("render_chart", {
                    "chart_type": "donut", "title": "x",
                    "data": [{"a": 1}], "x_key": "a", "y_keys": ["a"],
                })
            assert "chart_type must be one of" in str(ei.value)
    finally:
        patcher.stop()


async def test_render_chart_rejects_missing_keys():
    mcp, patcher = _build_server()
    try:
        async with Client(mcp) as client:
            with pytest.raises(Exception) as ei:
                await client.call_tool("render_chart", {
                    "chart_type": "line", "title": "x",
                    "data": [{"month": "Jan", "sales": 1}],
                    "x_key": "month", "y_keys": ["revenue"],
                })
            assert "not present in the data rows" in str(ei.value)
    finally:
        patcher.stop()


async def test_render_chart_rejects_empty_data():
    mcp, patcher = _build_server()
    try:
        async with Client(mcp) as client:
            with pytest.raises(Exception) as ei:
                await client.call_tool("render_chart", {
                    "chart_type": "bar", "title": "x",
                    "data": [], "x_key": "a", "y_keys": ["b"],
                })
            assert "non-empty list" in str(ei.value)
    finally:
        patcher.stop()
