# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Payload tests for the use-case tools.

Each test calls a tool through the MCP protocol and verifies the exact HTTP
request that would be sent to Viya — URL path, method, body, params — and
the shape of what comes back. No network calls.
"""

from unittest.mock import AsyncMock, patch

from fastmcp import Client

from conftest import _make_404_response, _make_mock_response, route_get, stub_compute_session
from sas_mcp_server.tools import TOOL_NAMES

# Tools that were removed in the use-case refocus — they must NOT come back.
REMOVED_TOOLS = [
    "execute_sas_code",
    "query_table",
    "get_castable_info",
    "get_castable_columns",
    "get_castable_data",
    "list_models_and_decisions",
    "list_ml_projects",
    "create_ml_project",
    "run_ml_project",
    "get_ml_project_results",
    "delete_ml_project",
    "list_cas_servers",
    "list_caslibs",
    "list_castables",
    "upload_data",
    "list_reports",
    "submit_batch_job",
]

STEP_SCORE = {
    "id": "score",
    "inputs": [
        {"name": "AGE", "type": "decimal"},
        {"name": "GENDER", "type": "string"},
        {"name": "VISITS", "type": "integer"},
    ],
    "outputs": [{"name": "EM_EVENTPROBABILITY", "type": "decimal"}, {"name": "EM_CLASSIFICATION", "type": "string"}],
}


def _stub_model(mock_client, module_id="readmission_gb", name="Readmission GB", steps=None):
    """Route the module GET, its steps, and the scoring POST."""
    steps = steps if steps is not None else [STEP_SCORE]
    route_get(
        mock_client,
        [
            (
                f"/microanalyticScore/modules/{module_id}/steps",
                _make_mock_response({"items": steps, "count": len(steps)}),
            ),
            (f"/microanalyticScore/modules/{module_id}", _make_mock_response({"id": module_id, "name": name})),
        ],
    )
    score_resp = _make_mock_response(
        {
            "moduleId": module_id,
            "stepId": "score",
            "executionState": "completed",
            "outputs": [{"name": "EM_EVENTPROBABILITY", "value": 0.42}, {"name": "EM_CLASSIFICATION", "value": "1"}],
        },
        status_code=201,
    )
    score_resp.content = b"{}"
    mock_client.post.return_value = score_resp


# -----------------------------------------------------------------------
# Tool surface
# -----------------------------------------------------------------------


async def test_all_tools_registered(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == set(TOOL_NAMES), f"unexpected tool set: {sorted(names)}"
    assert len(TOOL_NAMES) == 8


async def test_removed_tools_are_gone(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    for removed in REMOVED_TOOLS:
        assert removed not in names, f"Tool '{removed}' should be removed"


async def test_tool_schemas_and_annotations(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    def required(name):
        return tools[name].input_schema.get("required", [])

    assert required("get_use_case") == []
    assert required("describe_table") == [] and "table" in tools["describe_table"].input_schema["properties"]
    assert required("preview_table") == []
    assert required("query_data") == ["query"]
    assert required("list_models") == []
    assert required("describe_model") == ["model"]
    assert set(required("score_data")) == {"model", "input_data"}
    assert "step_id" in tools["score_data"].input_schema["properties"]
    assert set(required("render_chart")) == {"chart_type", "title", "data", "x_key", "y_keys"}

    # score_data accepts one record or a list of them.
    assert "anyOf" in tools["score_data"].input_schema["properties"]["input_data"]

    for name in TOOL_NAMES:
        ann = tools[name].annotations
        assert ann is not None, name
        assert ann.read_only_hint is (name != "score_data"), name
        assert ann.destructive_hint is False


# -----------------------------------------------------------------------
# describe_table / preview_table
# -----------------------------------------------------------------------


async def test_describe_table_request_and_shape(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    route_get(
        mock_client,
        [
            (
                "/tables/HMEQ/columns",
                _make_mock_response(
                    {"items": [{"name": "BAD", "type": "double", "label": "", "format": ""}], "count": 1}
                ),
            ),
            ("/tables/HMEQ", _make_mock_response({"rows": 5960, "columns": 13, "state": "loaded"})),
        ],
    )
    async with Client(mcp) as client:
        result = (await client.call_tool("describe_table", {"table": "Public.HMEQ"})).data

    urls = [c[0][0] for c in mock_client.get.call_args_list]
    assert any(u.endswith("/casManagement/servers/cas-shared-default/caslibs/Public/tables/HMEQ") for u in urls)
    assert any(u.endswith("/casManagement/servers/cas-shared-default/caslibs/Public/tables/HMEQ/columns") for u in urls)
    assert result["table"] == "Public.HMEQ"
    assert result["rowCount"] == 5960
    assert result["columnCount"] == 13
    assert result["columns"][0]["name"] == "BAD"


async def test_describe_table_accepts_three_part_names(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("describe_table", {"table": "cas1.Sales.ORDERS"})
    urls = [c[0][0] for c in mock_client.get.call_args_list]
    assert any("/casManagement/servers/cas1/caslibs/Sales/tables/ORDERS" in u for u in urls)


async def test_describe_table_missing_table_is_structured(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_404_response()
    async with Client(mcp) as client:
        result = (await client.call_tool("describe_table", {"table": "Public.NOPE"})).data
    assert result["status"] == "not_found"
    assert "NOPE" in result["message"]


async def test_describe_table_without_scope_or_table_explains(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        result = (await client.call_tool("describe_table", {})).data
    assert result["status"] == "no_table"


async def test_preview_table_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    route_get(
        mock_client,
        [
            ("/dataTables/dataSources/", _make_mock_response({"items": [{"name": "x"}, {"name": "y"}], "count": 2})),
            (
                "/rowSets/tables/",
                _make_mock_response({"items": [{"cells": ["1", "2"]}, {"cells": ["3", "4"]}], "count": 50}),
            ),
        ],
    )
    async with Client(mcp) as client:
        result = (await client.call_tool("preview_table", {"table": "Public.HMEQ", "limit": 2, "start": 10})).data

    calls = mock_client.get.call_args_list
    col_call = next(c for c in calls if "/dataTables/dataSources/" in c[0][0])
    row_call = next(c for c in calls if "/rowSets/tables/" in c[0][0])
    assert "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ/columns" in col_call[0][0]
    assert "/rowSets/tables/cas~fs~cas-shared-default~fs~Public~fs~HMEQ/rows" in row_call[0][0]
    assert row_call[1]["params"] == {"start": 10, "limit": 2}
    assert result["columns"] == ["x", "y"]
    assert result["rows"] == [{"x": "1", "y": "2"}, {"x": "3", "y": "4"}]
    assert result["count"] == 50
    assert result["truncated"] is True
    assert result["table"] == "Public.HMEQ"


async def test_preview_table_clamps_limit(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    route_get(
        mock_client,
        [
            ("/dataTables/dataSources/", _make_mock_response({"items": [{"name": "x"}], "count": 1})),
            ("/rowSets/tables/", _make_mock_response({"items": [], "count": 0})),
        ],
    )
    async with Client(mcp) as client:
        await client.call_tool("preview_table", {"table": "Public.HMEQ", "limit": 99999})
    row_call = next(c for c in mock_client.get.call_args_list if "/rowSets/tables/" in c[0][0])
    assert row_call[1]["params"]["limit"] == 1000


# -----------------------------------------------------------------------
# list_models / describe_model / score_data
# -----------------------------------------------------------------------


async def test_list_models_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"items": [{"id": "m1", "name": "Model One", "description": "d"}], "count": 1}
    )
    async with Client(mcp) as client:
        result = (await client.call_tool("list_models", {})).data
    assert "/microanalyticScore/modules" in mock_client.get.call_args[0][0]
    assert result == {"models": [{"id": "m1", "name": "Model One", "description": "d"}], "unavailable": []}


async def test_describe_model_returns_signature(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    _stub_model(mock_client)
    async with Client(mcp) as client:
        result = (await client.call_tool("describe_model", {"model": "readmission_gb"})).data
    assert result["id"] == "readmission_gb"
    assert result["stepId"] == "score"
    assert [i["name"] for i in result["inputs"]] == ["AGE", "GENDER", "VISITS"]
    assert [o["name"] for o in result["outputs"]] == ["EM_EVENTPROBABILITY", "EM_CLASSIFICATION"]
    assert result["steps"] == ["score"]


async def test_describe_model_prefers_score_then_execute(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    steps = [{"id": "execute", "inputs": [], "outputs": []}, {"id": "other", "inputs": [], "outputs": []}]
    _stub_model(mock_client, module_id="dec1", name="Decision", steps=steps)
    async with Client(mcp) as client:
        result = (await client.call_tool("describe_model", {"model": "dec1"})).data
    assert result["stepId"] == "execute"


async def test_describe_model_unknown_is_structured(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    route_get(mock_client, [("/microanalyticScore/modules/nope", _make_404_response())])
    async with Client(mcp) as client:
        result = (await client.call_tool("describe_model", {"model": "nope"})).data
    assert result["status"] == "not_found"


async def test_score_data_request_maps_and_coerces_inputs(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    _stub_model(mock_client)
    async with Client(mcp) as client:
        result = (
            await client.call_tool(
                "score_data",
                {"model": "readmission_gb", "input_data": {"age": "35", "gender": "F", "visits": 2.0, "extra": 1}},
            )
        ).data

    url = mock_client.post.call_args[0][0]
    assert url.endswith("/microanalyticScore/modules/readmission_gb/steps/score")
    body = mock_client.post.call_args[1]["json"]
    assert body["inputs"] == [
        {"name": "AGE", "value": 35.0},
        {"name": "GENDER", "value": "F"},
        {"name": "VISITS", "value": 2},
    ]
    assert result["model"] == "readmission_gb"
    assert result["stepId"] == "score"
    assert result["outputs"] == {"EM_EVENTPROBABILITY": 0.42, "EM_CLASSIFICATION": "1"}
    assert result["ignoredInputs"] == ["extra"]
    assert result["missingInputs"] == []
    assert result["executionState"] == "completed"


async def test_score_data_reports_missing_inputs(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    _stub_model(mock_client)
    async with Client(mcp) as client:
        result = (await client.call_tool("score_data", {"model": "readmission_gb", "input_data": {"AGE": 40}})).data
    assert result["missingInputs"] == ["GENDER", "VISITS"]


async def test_score_data_scores_a_list_of_records(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    _stub_model(mock_client)
    async with Client(mcp) as client:
        result = (
            await client.call_tool(
                "score_data",
                {"model": "readmission_gb", "input_data": [{"AGE": 40}, {"AGE": 50, "GENDER": "M"}]},
            )
        ).data
    assert mock_client.post.call_count == 2
    assert result["count"] == 2
    assert result["results"][1]["inputsUsed"] == {"AGE": 50.0, "GENDER": "M"}


async def test_score_data_accepts_json_encoded_input(mcp_server_with_mock_client):
    """Some clients send object parameters as JSON strings; the tool absorbs that."""
    mcp, mock_client = mcp_server_with_mock_client
    _stub_model(mock_client)
    async with Client(mcp) as client:
        result = (await client.call_tool("score_data", {"model": "readmission_gb", "input_data": '{"AGE": 61}'})).data
    assert result["inputsUsed"] == {"AGE": 61.0}


async def test_score_data_resolves_model_by_display_name(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    _stub_model(mock_client)
    # A GET by the display name fails; the module list resolves it.
    route_get(
        mock_client,
        [
            (
                "/microanalyticScore/modules/readmission_gb/steps",
                _make_mock_response({"items": [STEP_SCORE], "count": 1}),
            ),
            ("/microanalyticScore/modules/readmission%20gb", _make_404_response()),
            ("/microanalyticScore/modules/readmission gb", _make_404_response()),
            (
                "/microanalyticScore/modules",
                _make_mock_response({"items": [{"id": "readmission_gb", "name": "Readmission GB"}], "count": 1}),
            ),
        ],
    )
    async with Client(mcp) as client:
        result = (await client.call_tool("score_data", {"model": "Readmission GB", "input_data": {"AGE": 1}})).data
    assert result["model"] == "readmission_gb"
    assert mock_client.post.call_args[0][0].endswith("/modules/readmission_gb/steps/score")


async def test_score_data_unknown_model_is_structured(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    route_get(mock_client, [("/microanalyticScore/modules/nope", _make_404_response())])
    async with Client(mcp) as client:
        result = (await client.call_tool("score_data", {"model": "nope", "input_data": {"a": 1}})).data
    assert result["status"] == "not_found"
    mock_client.post.assert_not_called()


async def test_score_data_explicit_step(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    steps = [STEP_SCORE, {"id": "execute", "inputs": [{"name": "A", "type": "decimal"}], "outputs": []}]
    _stub_model(mock_client, steps=steps)
    async with Client(mcp) as client:
        await client.call_tool("score_data", {"model": "readmission_gb", "input_data": {"a": 1}, "step_id": "execute"})
    assert mock_client.post.call_args[0][0].endswith("/steps/execute")


# -----------------------------------------------------------------------
# query_data (FedSQL) — generated program and result shaping
# -----------------------------------------------------------------------


async def test_query_data_generates_capped_fedsql_and_shapes_rows(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    stub_compute_session(mock_client)
    submitted: dict[str, str] = {}

    async def fake_run_job(client, session_id, code, poll=2):
        submitted["code"] = code
        submitted["session"] = session_id
        return "completed", "NOTE: PROCEDURE FEDSQL used", ""

    async def fake_paged(path, client, **kwargs):
        if path.endswith("/columns"):
            return [
                {"name": "d", "type": "FLOAT", "format": {"name": "DATE9."}},
                {"name": "amt", "type": "FLOAT"},
            ], 2
        return [{"cells": [24107, 1234.5]}], 1

    with (
        patch("sas_mcp_server.tools.data.run_job", side_effect=fake_run_job),
        patch("sas_mcp_server.tools.data.get_paged_items", side_effect=fake_paged),
        patch("sas_mcp_server.tools.data.submit_job", new=AsyncMock(return_value="CLEANUP")),
    ):
        async with Client(mcp) as client:
            result = (await client.call_tool("query_data", {"query": "select d, amt from Public.T", "limit": 5})).data

    assert submitted["session"] == "sess-id"
    assert "limit 6" in submitted["code"]  # limit + 1 truncation probe
    assert "cas _mcpq" in submitted["code"] and "terminate;" in submitted["code"]
    assert result["columns"] == ["d", "amt"]
    assert result["rows"] == [{"d": "2026-01-01", "amt": 1234.5}]
    assert result["count"] == 1
    assert result["truncated"] is False
    assert result["column_types"] == {"d": "FLOAT", "amt": "FLOAT"}
    assert result["query"] == "select d, amt from Public.T"


async def test_query_data_reuses_the_compute_session(mcp_server_with_mock_client):
    """The second query must not create a second session."""
    mcp, mock_client = mcp_server_with_mock_client
    stub_compute_session(mock_client)
    state_resp = _make_mock_response({}, status_code=200)
    route_get(
        mock_client,
        [
            ("/compute/contexts", _make_mock_response({"items": [{"id": "ctx-id"}]})),
            ("/compute/sessions/sess-id/state", state_resp),
        ],
    )

    async def fake_run_job(client, session_id, code, poll=2):
        return "completed", "", ""

    async def fake_paged(path, client, **kwargs):
        return ([{"name": "a", "type": "FLOAT"}], 1) if path.endswith("/columns") else ([], 0)

    with (
        patch("sas_mcp_server.tools.data.run_job", side_effect=fake_run_job),
        patch("sas_mcp_server.tools.data.get_paged_items", side_effect=fake_paged),
        patch("sas_mcp_server.tools.data.submit_job", new=AsyncMock(return_value="C")),
    ):
        async with Client(mcp) as client:
            await client.call_tool("query_data", {"query": "select a from Public.T"})
            await client.call_tool("query_data", {"query": "select a from Public.T"})
    session_creates = [c for c in mock_client.post.call_args_list if "/sessions" in c[0][0]]
    assert len(session_creates) == 1


async def test_query_data_flags_truncation_from_the_extra_row(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    stub_compute_session(mock_client)
    asked: dict[str, int] = {}

    async def fake_paged(path, client, **kwargs):
        if path.endswith("/columns"):
            return [{"name": "a", "type": "FLOAT"}], 1
        asked["limit"] = kwargs["limit"]
        return [{"cells": [1]}, {"cells": [2]}, {"cells": [3]}], 3

    with (
        patch("sas_mcp_server.tools.data.run_job", new=AsyncMock(return_value=("completed", "", ""))),
        patch("sas_mcp_server.tools.data.get_paged_items", side_effect=fake_paged),
        patch("sas_mcp_server.tools.data.submit_job", new=AsyncMock(return_value="C")),
    ):
        async with Client(mcp) as client:
            result = (await client.call_tool("query_data", {"query": "select a from Public.T", "limit": 2})).data

    assert asked["limit"] == 3
    assert result["truncated"] is True
    assert result["count"] == 2
    assert result["rows"] == [{"a": 1}, {"a": 2}]


async def test_query_data_maps_a_sas_error_without_raising(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    stub_compute_session(mock_client)
    log = 'ERROR: Table "PUBLIC.NOPE" does not exist or cannot be accessed'
    with (
        patch("sas_mcp_server.tools.data.run_job", new=AsyncMock(return_value=("completed", log, ""))),
        patch("sas_mcp_server.tools.data.submit_job", new=AsyncMock(return_value="C")),
    ):
        async with Client(mcp) as client:
            result = (await client.call_tool("query_data", {"query": "select a from Public.NOPE"})).data
    assert result["status"] == "table_not_found"
    assert "PUBLIC.NOPE" in result["message"]


async def test_query_data_refuses_writes_before_touching_viya(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    with patch("sas_mcp_server.tools.data.run_job", new=AsyncMock()) as run:
        async with Client(mcp) as client:
            result = (await client.call_tool("query_data", {"query": "delete from Public.T"})).data
    assert result["status"] == "invalid_query"
    assert "DELETE" in result["message"]
    run.assert_not_awaited()
    mock_client.post.assert_not_called()


async def test_query_data_qualifies_bare_table_and_blocks_out_of_scope(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_TABLES": "Public.PATIENTS"})
    stub_compute_session(mock_client)
    submitted: dict[str, str] = {}

    async def fake_run_job(client, session_id, code, poll=2):
        submitted["code"] = code
        return "completed", "", ""

    async def fake_paged(path, client, **kwargs):
        return ([{"name": "n", "type": "FLOAT"}], 1) if path.endswith("/columns") else ([{"cells": [3]}], 1)

    with (
        patch("sas_mcp_server.tools.data.run_job", side_effect=fake_run_job),
        patch("sas_mcp_server.tools.data.get_paged_items", side_effect=fake_paged),
        patch("sas_mcp_server.tools.data.submit_job", new=AsyncMock(return_value="C")),
    ):
        async with Client(mcp) as client:
            ok = (await client.call_tool("query_data", {"query": "select count(*) as n from patients p"})).data
            blocked = (await client.call_tool("query_data", {"query": "select * from Public.SECRET"})).data

    assert "from Public.PATIENTS p" in submitted["code"]
    assert ok["query"] == "select count(*) as n from Public.PATIENTS p"
    assert ok["rows"] == [{"n": 3}]
    assert blocked["status"] == "out_of_scope"
    assert "Public.PATIENTS" in blocked["message"]
