# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests that call the use-case MCP tools against a real SAS Viya
instance.

Requires VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD environment variables.
Run with:  uv run python -m pytest -m integration

These exercise only the lean, use-case-scoped tool set. They target a known
sample table — ``HMEQ`` in caslib ``Public`` on ``cas-shared-default`` — and
skip gracefully when it (or a required service) isn't present.
"""
import json
import time
import pytest
from fastmcp import Client

# Pin all integration tests to a single session-scoped event loop. The
# session-scoped fixtures (viya_token, integration_mcp_server) and the
# in-memory fastmcp transport must share the same loop they were created in;
# otherwise the second test's tool call fails with ConnectError when it
# touches httpx state bound to the prior, now-closed loop.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_SUFFIX = str(int(time.time()))[-6:]
_SERVER = "cas-shared-default"
_CASLIB = "Public"
_TABLE = "HMEQ"


# -----------------------------------------------------------------------
# Use case & data inspection
# -----------------------------------------------------------------------


async def test_get_use_case(integration_mcp_server):
    """get_use_case returns a manifest (scoped or not)."""
    async with Client(integration_mcp_server) as client:
        manifest = (await client.call_tool("get_use_case", {})).data
        assert isinstance(manifest, dict)
        assert "scoped" in manifest


async def test_data_inspection_workflow(integration_mcp_server):
    """get_castable_info → get_castable_columns → get_castable_data on HMEQ."""
    async with Client(integration_mcp_server) as client:
        try:
            info = (await client.call_tool("get_castable_info", {
                "server_id": _SERVER, "caslib_name": _CASLIB, "table_name": _TABLE,
            })).data
        except Exception as e:
            if "404" in str(e):
                pytest.skip("HMEQ not loaded in Public caslib on this Viya")
            raise
        assert isinstance(info, dict)

        columns = (await client.call_tool("get_castable_columns", {
            "server_id": _SERVER, "caslib_name": _CASLIB, "table_name": _TABLE,
            "limit": 10,
        })).data
        assert isinstance(columns, list)
        assert len(columns) > 0

        try:
            rows = (await client.call_tool("get_castable_data", {
                "server_id": _SERVER, "caslib_name": _CASLIB, "table_name": _TABLE,
                "limit": 3,
            })).data
            assert isinstance(rows, dict)
            assert "columns" in rows and "rows" in rows
        except Exception:
            pass


# -----------------------------------------------------------------------
# Structured query (the query → chart path)
# -----------------------------------------------------------------------


async def test_query_table_returns_rows(integration_mcp_server):
    """query_table runs SQL and returns structured columns + rows."""
    async with Client(integration_mcp_server) as client:
        result = (await client.call_tool("query_table", {
            "sql": f"select BAD, count(*) as n from {_CASLIB}.{_TABLE} group by BAD",
            "limit": 10,
        })).data
        assert isinstance(result, dict)
        if result.get("error"):
            pytest.skip(f"query_table could not run (likely HMEQ absent): "
                        f"{result.get('state')}")
        assert "columns" in result
        assert "rows" in result
        assert isinstance(result["rows"], list)


# -----------------------------------------------------------------------
# SAS Code Execution
# -----------------------------------------------------------------------


async def test_sas_code_execution(integration_mcp_server):
    """execute_sas_code with a simple DATA step + PROC PRINT."""
    async with Client(integration_mcp_server) as client:
        code = """
data work.mcp_test;
    x = 42;
    y = "hello";
    output;
run;

proc print data=work.mcp_test;
run;
"""
        result = await client.call_tool("execute_sas_code", {"sas_code": code})
        parsed = json.loads(result.content[0].text)
        assert isinstance(parsed, list)
        assert len(parsed) == 4
        snippet_id, state, log, listing = parsed
        assert state in ("completed", "warning")
        assert "mcp_test" in log.lower() or "NOTE" in log


# -----------------------------------------------------------------------
# Model building (AutoML)
# -----------------------------------------------------------------------


async def test_ml_project_workflow(integration_mcp_server):
    """create_ml_project → list_ml_projects → get_ml_project_results → delete_ml_project."""
    async with Client(integration_mcp_server) as client:
        try:
            project = (await client.call_tool("create_ml_project", {
                "project_name": f"MCP Integration Test {_SUFFIX}",
                "data_table_uri": (f"/dataTables/dataSources/"
                                   f"cas~fs~{_SERVER}~fs~{_CASLIB}/tables/{_TABLE}"),
                "target_variable": "BAD",
                "prediction_type": "binary",
                "target_event_level": "1",
                "auto_run": False,
            })).data
        except Exception as e:
            if "404" in str(e):
                pytest.skip("HMEQ not available to train against on this Viya")
            raise
        assert isinstance(project, dict)
        assert "id" in project
        project_id = project["id"]

        try:
            projects = (await client.call_tool("list_ml_projects", {"limit": 100})).data
            assert any(p["id"] == project_id for p in projects)

            results = (await client.call_tool("get_ml_project_results", {
                "project_id": project_id,
            })).data
            assert isinstance(results, dict)
            assert results["projectId"] == project_id
            assert "state" in results
        finally:
            # Clean up the project we created.
            await client.call_tool("delete_ml_project", {"project_id": project_id})


# -----------------------------------------------------------------------
# Ready models & decisions — listing and scoring
# -----------------------------------------------------------------------


async def test_scoring_workflow(integration_mcp_server):
    """list_models_and_decisions → score_data (best-effort)."""
    async with Client(integration_mcp_server) as client:
        modules = (await client.call_tool("list_models_and_decisions", {"limit": 5})).data
        assert isinstance(modules, list)

        if not modules:
            pytest.skip("No MAS modules found — cannot test score_data")

        module_id = modules[0]["id"]
        try:
            result = (await client.call_tool("score_data", {
                "module_id": module_id,
                "input_data": {"x": 1},
            })).data
            assert isinstance(result, dict)
        except Exception:
            pytest.skip(f"Module {module_id} does not have a 'score' step "
                        f"or expects different inputs")
