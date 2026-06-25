# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Payload assertion tests for the use-case MCP tools.

Each test calls a tool through the MCP protocol and verifies the exact HTTP
request that would be sent to Viya — URL path, method, body structure, query
params, and headers.  These tests use a mock httpx client (no network calls).
"""
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp import Client
from conftest import _make_mock_response


# The lean, use-case-scoped tool set.
EXPECTED_TOOLS = [
    "get_use_case",
    "execute_sas_code", "query_table",
    "render_chart",
    "get_castable_info", "get_castable_columns", "get_castable_data",
    "list_models_and_decisions", "score_data",
    "list_ml_projects", "create_ml_project", "run_ml_project",
    "get_ml_project_results", "delete_ml_project",
]

# Tools that were intentionally removed when scoping this server to a single
# use case — they must NOT come back.
REMOVED_TOOLS = [
    "list_cas_servers", "list_caslibs", "list_castables",
    "upload_data", "promote_table_to_memory",
    "list_files", "upload_file", "download_file",
    "list_reports", "get_report", "get_report_image",
    "submit_batch_job", "get_job_status", "list_jobs",
    "cancel_job", "get_job_log",
    "list_registered_models",
    "get_report_content", "create_report", "update_report_content",
    "validate_report_content", "delete_report", "create_report_from_template",
    "export_report_pdf", "get_export_job", "explain_data",
]


# -----------------------------------------------------------------------
# Schema validation
# -----------------------------------------------------------------------


async def test_all_tools_registered(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert names == set(EXPECTED_TOOLS), (
            f"unexpected tool set: {sorted(names)}")


async def test_removed_tools_are_gone(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        for removed in REMOVED_TOOLS:
            assert removed not in names, f"Tool '{removed}' should be removed"


async def test_tool_schemas(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        tools = await client.list_tools()
        tool_map = {t.name: t for t in tools}

        create_ml = tool_map["create_ml_project"]
        props = create_ml.inputSchema["properties"]
        for p in ("project_name", "data_table_uri", "target_variable",
                  "prediction_type", "target_event_level", "auto_run"):
            assert p in props
        required = create_ml.inputSchema.get("required", [])
        assert "project_name" in required
        assert "data_table_uri" in required
        assert "target_variable" in required

        score = tool_map["score_data"]
        props = score.inputSchema["properties"]
        assert "module_id" in props
        assert "step_id" in props
        assert "input_data" in props
        # step_id is now optional (defaults to "score").
        assert "step_id" not in score.inputSchema.get("required", [])

        query = tool_map["query_table"]
        props = query.inputSchema["properties"]
        assert "sql" in props
        assert "limit" in props
        assert "sql" in query.inputSchema.get("required", [])

        # Data tools default to the use-case table — none of the table
        # coordinates may be required.
        cols = tool_map["get_castable_columns"]
        required = cols.inputSchema.get("required", [])
        for p in ("table_name", "caslib_name", "server_id"):
            assert p in cols.inputSchema["properties"]
            assert p not in required


# -----------------------------------------------------------------------
# SAS execution & structured query
# -----------------------------------------------------------------------


async def test_execute_sas_code_request(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    with patch("sas_mcp_server.tools.run_one_snippet") as mock_run:
        mock_run.return_value = ("1", "completed", "LOG", "LISTING")
        async with Client(mcp) as client:
            await client.call_tool("execute_sas_code", {
                "sas_code": "data test; x=1; run;"
            })
        mock_run.assert_called_once_with("data test; x=1; run;", "1", "test-token")


async def test_query_table_request(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    fake = {"error": False, "state": "completed",
            "columns": ["nationality", "n"],
            "rows": [{"nationality": "A", "n": 10}], "rowCount": 1}
    with patch("sas_mcp_server.tools.run_query_rows",
               new=AsyncMock(return_value=fake)) as mock_q:
        async with Client(mcp) as client:
            result = await client.call_tool("query_table", {
                "sql": "select nationality, count(*) as n from Public.T group by 1",
                "limit": 5,
            })
        mock_q.assert_awaited_once()
        call = mock_q.await_args
        assert call.args[0].startswith("select nationality")
        assert call.args[1] == "test-token"
        assert call.kwargs["limit"] == 5
        assert result.data["columns"] == ["nationality", "n"]
        assert result.data["rows"] == [{"nationality": "A", "n": 10}]


async def test_query_table_error_truncates_log(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    fake = {"error": True, "state": "error", "log": "ERROR: bad sql",
            "columns": [], "rows": []}
    with patch("sas_mcp_server.tools.run_query_rows",
               new=AsyncMock(return_value=fake)):
        async with Client(mcp) as client:
            result = await client.call_tool("query_table", {"sql": "select oops"})
        assert result.data["error"] is True
        assert "ERROR" in result.data["log"]


# -----------------------------------------------------------------------
# Data inspection (auto-scoped to the use-case table)
# -----------------------------------------------------------------------


async def test_get_castable_info_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_castable_info", {
            "server_id": "cas1", "caslib_name": "Public", "table_name": "HMEQ"
        })

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ" in url
    headers = mock_client.get.call_args[1]["headers"]
    assert headers["Accept"] == "application/json"


async def test_get_castable_columns_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_castable_columns", {
            "server_id": "cas1", "caslib_name": "Public",
            "table_name": "HMEQ", "limit": 100
        })

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ/columns" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 100


async def test_get_castable_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client

    col_resp = _make_mock_response({
        "items": [
            {"name": "x", "type": "double", "index": 0},
            {"name": "y", "type": "double", "index": 1},
        ],
        "count": 2,
    })
    row_resp = _make_mock_response({
        "items": [{"cells": ["1", "2"]}, {"cells": ["3", "4"]}],
        "count": 2,
    })

    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/dataTables/dataSources/" in url and "/columns" in url:
            return col_resp
        if "/rowSets/tables/" in url and "/rows" in url:
            return row_resp
        return original_get

    mock_client.get.side_effect = route_get

    async with Client(mcp) as client:
        result = await client.call_tool("get_castable_data", {
            "server_id": "cas1", "caslib_name": "Public",
            "table_name": "HMEQ", "limit": 5, "start": 10
        })

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    calls = mock_client.get.call_args_list
    col_call = next(c for c in calls if "/dataTables/dataSources/" in c[0][0])
    row_call = next(c for c in calls if "/rowSets/tables/" in c[0][0])

    assert "/dataTables/dataSources/cas~fs~cas1~fs~Public/tables/HMEQ/columns" in col_call[0][0]
    assert col_call[1]["params"]["limit"] == 100

    assert "/rowSets/tables/cas~fs~cas1~fs~Public~fs~HMEQ/rows" in row_call[0][0]
    assert row_call[1]["params"] == {"start": 10, "limit": 5}

    assert result.data["columns"] == ["x", "y"]
    assert result.data["rows"] == [{"x": "1", "y": "2"}, {"x": "3", "y": "4"}]


# -----------------------------------------------------------------------
# Ready models & decisions — listing and scoring
# -----------------------------------------------------------------------


async def test_list_models_and_decisions_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_models_and_decisions", {})

    url = mock_client.get.call_args[0][0]
    assert "/microanalyticScore/modules" in url


async def test_score_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("score_data", {
            "module_id": "mod-1",
            "step_id": "score",
            "input_data": {"age": 35, "income": 50000}
        })

    url = mock_client.post.call_args[0][0]
    assert "/microanalyticScore/modules/mod-1/steps/score" in url
    body = mock_client.post.call_args[1]["json"]
    assert "inputs" in body
    input_values = {inp["name"]: inp["value"] for inp in body["inputs"]}
    assert input_values == {"age": 35, "income": 50000}


async def test_score_data_default_step(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("score_data", {
            "module_id": "mod-1", "input_data": {"x": 1}
        })

    url = mock_client.post.call_args[0][0]
    assert "/microanalyticScore/modules/mod-1/steps/score" in url


# -----------------------------------------------------------------------
# Model building (AutoML pipeline automation)
# -----------------------------------------------------------------------


async def test_list_ml_projects_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_ml_projects", {"limit": 10})

    url = mock_client.get.call_args[0][0]
    assert "/mlPipelineAutomation/projects" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 10


async def test_create_ml_project_binary_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "Fraud Detection",
            "data_table_uri": "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ",
            "target_variable": "BAD",
            "description": "Binary classification project",
            "prediction_type": "binary",
            "target_event_level": "1",
        })

    url = mock_client.post.call_args[0][0]
    assert "/mlPipelineAutomation/projects" in url
    body = mock_client.post.call_args[1]["json"]

    assert body["name"] == "Fraud Detection"
    assert body["type"] == "predictive"
    assert body["dataTableUri"].endswith("/tables/HMEQ")
    assert body["pipelineBuildMethod"] == "automatic"

    settings = body["settings"]
    assert settings["applyGlobalMetadata"] is False
    assert settings["autoRun"] is True
    assert settings["numberOfModels"] == 5

    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetVariable"] == "BAD"
    assert attrs["partitionEnabled"] is True
    assert attrs["targetEventLevel"] == "1"
    # targetLevel / classSelectionStatistic are NOT valid project attributes
    # and cause the MLPA metadata step to fail — they must not be sent.
    assert "targetLevel" not in attrs
    assert "classSelectionStatistic" not in attrs

    accept = mock_client.post.call_args[1]["headers"]["Accept"]
    assert accept == "application/vnd.sas.analytics.ml.pipeline.automation.project+json"

    assert "predictionType" not in body
    assert "predictionType" not in attrs


async def test_create_ml_project_interval_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "Price Prediction",
            "data_table_uri": "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/CARS",
            "target_variable": "MSRP",
            "prediction_type": "interval",
        })

    body = mock_client.post.call_args[1]["json"]
    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetVariable"] == "MSRP"
    assert "targetEventLevel" not in attrs  # not sent for interval targets


async def test_create_ml_project_nominal_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "Multi Class",
            "data_table_uri": "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/IRIS",
            "target_variable": "Species",
            "prediction_type": "nominal",
            "target_event_level": "setosa",
        })

    body = mock_client.post.call_args[1]["json"]
    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetVariable"] == "Species"
    assert attrs["targetEventLevel"] == "setosa"


async def test_create_ml_project_auto_run_false(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "No Auto Run",
            "data_table_uri": "/dataTables/dataSources/x/tables/T",
            "target_variable": "Y",
            "auto_run": False,
        })

    body = mock_client.post.call_args[1]["json"]
    assert body["settings"]["autoRun"] is False


async def test_run_ml_project_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value.headers = {"etag": '"test-etag"', "Content-Type": "application/json"}
    mock_client.get.return_value.json = MagicMock(return_value={"id": "proj-123", "name": "Test"})
    async with Client(mcp) as client:
        await client.call_tool("run_ml_project", {"project_id": "proj-123"})

    get_url = mock_client.get.call_args[0][0]
    assert "/mlPipelineAutomation/projects/proj-123" in get_url

    put_url = mock_client.put.call_args[0][0]
    assert "/mlPipelineAutomation/projects/proj-123" in put_url
    params = mock_client.put.call_args[1]["params"]
    assert params == {"action": "retrainProject"}
    headers = mock_client.put.call_args[1]["headers"]
    assert headers["If-Match"] == '"test-etag"'
    assert headers["Accept-Language"] == "en"


async def test_get_ml_project_results_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client

    proj_resp = _make_mock_response({
        "name": "Fraud", "state": "completed",
        "championModel": {"name": "Gradient Boosting"},
    })
    models_resp = _make_mock_response({
        "items": [
            {"name": "GB", "algorithmName": "Gradient Boosting",
             "champion": True, "fitStatistics": {"ks": 0.62}},
            {"name": "LR", "algorithmName": "Logistic Regression",
             "champion": False, "fitStatistics": {"ks": 0.55}},
        ],
        "count": 2,
    })

    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if url.endswith("/mlPipelineAutomation/projects/proj-1"):
            return proj_resp
        if "/mlPipelineAutomation/projects/proj-1/models" in url:
            return models_resp
        return original_get

    mock_client.get.side_effect = route_get

    async with Client(mcp) as client:
        result = await client.call_tool("get_ml_project_results",
                                        {"project_id": "proj-1"})

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    assert result.data["state"] == "completed"
    assert result.data["championModel"]["name"] == "Gradient Boosting"
    leaderboard = result.data["leaderboard"]
    assert len(leaderboard) == 2
    assert leaderboard[0]["algorithm"] == "Gradient Boosting"
    assert leaderboard[0]["champion"] is True


async def test_delete_ml_project_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("delete_ml_project", {"project_id": "proj-123"})

    url = mock_client.delete.call_args[0][0]
    assert "/mlPipelineAutomation/projects/proj-123" in url
