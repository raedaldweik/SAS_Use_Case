# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Use-case scoping: the allowlist logic, the auto-scope resolution that lets the
data tools default to the pinned table, and the guard behaviour of the tools."""

from fastmcp import Client

from conftest import _make_mock_response, route_get
from sas_mcp_server.usecase import UseCaseScope, _parse_list, load_scope, parse_table_spec

# ---------------------------------------------------------------------------
# UseCaseScope unit logic
# ---------------------------------------------------------------------------


def test_parse_list_handles_commas_newlines_and_spaces():
    assert _parse_list("a, b ,c") == ["a", "b", "c"]
    assert _parse_list("a\nb\n c ") == ["a", "b", "c"]
    assert _parse_list("") == []
    assert _parse_list(None) == []


def test_scope_inactive_by_default():
    s = UseCaseScope()
    assert s.active is False
    assert s.enforced is False
    assert s.allows_table("anything") is True
    assert s.allows_scoreable("anything") is True


def test_scope_active_when_any_allowlist_set():
    assert UseCaseScope(tables=["t1"]).active is True
    assert UseCaseScope(models=["m1"]).active is True
    assert UseCaseScope(decisions=["d1"]).active is True


def test_table_matching_supports_qualified_forms():
    s = UseCaseScope(tables=["Public.SALES"])
    assert s.allows_table(name="SALES", caslib="Public")
    assert s.allows_table(name="sales", caslib="public", server="cas-shared-default")
    s2 = UseCaseScope(tables=["SALES"])
    assert s2.allows_table(name="sales", caslib="Public")
    assert not s.allows_table(name="HR", caslib="Public")


def test_allows_scoreable_checks_union_of_models_and_decisions():
    s = UseCaseScope(decisions=["fraud_decision"])
    assert s.allows_scoreable("fraud_decision")
    assert not s.allows_scoreable("other_module")
    s2 = UseCaseScope(models=["risk_model"], decisions=["fraud_decision"])
    assert s2.allows_scoreable("risk_model")
    assert s2.allows_scoreable("zzz", "Fraud_Decision")  # any candidate, case-insensitive
    assert not s2.allows_scoreable("nope")
    assert UseCaseScope(tables=["t"]).allows_scoreable("anything") is True


def test_enforced_requires_active_and_enforce_flag():
    assert UseCaseScope(tables=["t"], enforce=True).enforced is True
    assert UseCaseScope(tables=["t"], enforce=False).enforced is False
    assert UseCaseScope(enforce=True).enforced is False


def test_parse_table_spec_supports_all_forms():
    assert parse_table_spec("srv.lib.TBL", "d", "Public") == {"server": "srv", "caslib": "lib", "table": "TBL"}
    assert parse_table_spec("lib2.TBL2", "d", "Public") == {"server": "d", "caslib": "lib2", "table": "TBL2"}
    assert parse_table_spec("TBL3", "d", "Public") == {"server": "d", "caslib": "Public", "table": "TBL3"}
    assert parse_table_spec('"Public"."My Table"', "d", "x") == {"server": "d", "caslib": "Public", "table": "My Table"}


def test_primary_table_and_qualified_names():
    s = UseCaseScope(tables=["Public.PATIENTS", "Public.VISITS"])
    assert s.primary_table == {"server": "cas-shared-default", "caslib": "Public", "table": "PATIENTS"}
    assert s.qualified_tables == ["Public.PATIENTS", "Public.VISITS"]
    assert UseCaseScope().primary_table is None


def test_resolve_fills_missing_from_primary_then_defaults():
    s = UseCaseScope(tables=["myserver.mylib.DRIVERS"])
    assert s.resolve() == ("myserver", "mylib", "DRIVERS")
    assert s.resolve(table="OTHER") == ("myserver", "mylib", "OTHER")
    assert s.resolve(server="s2", caslib="c2", table="T2") == ("s2", "c2", "T2")


def test_resolve_spec_parses_one_string():
    s = UseCaseScope(tables=["Public.PATIENTS"])
    assert s.resolve_spec(None) == ("cas-shared-default", "Public", "PATIENTS")
    assert s.resolve_spec("  ") == ("cas-shared-default", "Public", "PATIENTS")
    assert s.resolve_spec("VISITS") == ("cas-shared-default", "Public", "VISITS")
    assert s.resolve_spec("Sales.ORDERS") == ("cas-shared-default", "Sales", "ORDERS")
    assert s.resolve_spec("cas2.Sales.ORDERS") == ("cas2", "Sales", "ORDERS")


def test_find_table_by_bare_name():
    s = UseCaseScope(tables=["Public.PATIENTS", "Sales.ORDERS"])
    assert s.find_table("patients")["caslib"] == "Public"
    assert s.find_table("ORDERS")["caslib"] == "Sales"
    assert s.find_table("nope") is None


def test_manifest_contents():
    s = UseCaseScope(name="Pop Health", description="d", models=["m1"], decisions=["d1"], tables=["Public.PATIENTS"])
    m = s.manifest()
    assert m["useCaseName"] == "Pop Health"
    assert m["scoped"] is True
    assert m["enforced"] is True
    assert m["allowedModels"] == ["m1", "d1"]
    assert m["allowedTables"] == ["Public.PATIENTS"]
    assert m["defaultServer"] == "cas-shared-default"
    assert m["primaryTable"]["table"] == "PATIENTS"


def test_load_scope_reads_env(monkeypatch):
    monkeypatch.setenv("USE_CASE_NAME", "Population Health")
    monkeypatch.setenv("ALLOWED_MODELS", "readmission_gb")
    monkeypatch.setenv("ALLOWED_DECISIONS", "")
    monkeypatch.setenv("ALLOWED_TABLES", "Public.PATIENTS")
    monkeypatch.setenv("DEFAULT_CASLIB", "Public")
    monkeypatch.setenv("SCOPE_ENFORCE", "false")
    s = load_scope()
    assert s.name == "Population Health"
    assert s.models == ["readmission_gb"]
    assert s.tables == ["Public.PATIENTS"]
    assert s.active is True
    assert s.enforce is False
    assert s.enforced is False
    assert s.primary_table["table"] == "PATIENTS"


# ---------------------------------------------------------------------------
# Scoped server behaviour (through the MCP protocol)
# ---------------------------------------------------------------------------


def _stub_primary_table(mock_client):
    route_get(
        mock_client,
        [
            (
                "/tables/PATIENTS/columns",
                _make_mock_response(
                    {"items": [{"name": "age", "type": "double"}, {"name": "region", "type": "char"}], "count": 2}
                ),
            ),
            ("/tables/PATIENTS", _make_mock_response({"rows": 1200, "columns": 2, "state": "loaded"})),
            (
                "/microanalyticScore/modules/readmission_gb/steps",
                _make_mock_response(
                    {
                        "items": [
                            {
                                "id": "score",
                                "inputs": [{"name": "age", "type": "decimal"}],
                                "outputs": [{"name": "P", "type": "decimal"}],
                            }
                        ],
                        "count": 1,
                    }
                ),
            ),
            (
                "/microanalyticScore/modules",
                _make_mock_response(
                    {
                        "items": [{"id": "readmission_gb", "name": "Readmission GB"}, {"id": "other", "name": "Other"}],
                        "count": 2,
                    }
                ),
            ),
        ],
    )


async def test_get_use_case_grounds_the_agent(scoped_server):
    mcp, mock_client = scoped_server(
        {
            "USE_CASE_NAME": "Population Health",
            "ALLOWED_TABLES": "Public.PATIENTS",
            "ALLOWED_MODELS": "readmission_gb, not_yet_published",
        }
    )
    _stub_primary_table(mock_client)
    async with Client(mcp) as client:
        res = (await client.call_tool("get_use_case", {})).data
    assert res["useCaseName"] == "Population Health"
    assert res["scoped"] is True
    primary = res["primaryTable"]
    assert primary["qualifiedName"] == "Public.PATIENTS"
    assert primary["rowCount"] == 1200
    assert {c["name"] for c in primary["columns"]} == {"age", "region"}
    assert [m["id"] for m in res["models"]] == ["readmission_gb"]
    assert res["models"][0]["stepId"] == "score"
    assert res["models"][0]["inputs"][0]["name"] == "age"
    assert res["modelsUnavailable"] == ["not_yet_published"]
    assert res["guidance"]


async def test_get_use_case_never_fails_on_viya_errors(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_TABLES": "Public.PATIENTS", "ALLOWED_MODELS": "m"})
    mock_client.get.side_effect = RuntimeError("Viya down")
    async with Client(mcp) as client:
        res = (await client.call_tool("get_use_case", {})).data
    assert res["scoped"] is True
    assert "infoNote" in res["primaryTable"]
    assert "modelsNote" in res


async def test_data_tool_defaults_to_primary_table(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_TABLES": "Public.DRIVER_RISK_SCORE"})
    async with Client(mcp) as client:
        await client.call_tool("describe_table", {})
    urls = [c[0][0] for c in mock_client.get.call_args_list]
    assert any("/casManagement/servers/cas-shared-default/caslibs/Public/tables/DRIVER_RISK_SCORE" in u for u in urls)


async def test_describe_table_blocked_when_out_of_scope(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_TABLES": "Public.DRIVERS"})
    async with Client(mcp) as client:
        blocked = (await client.call_tool("describe_table", {"table": "Public.SECRET"})).data
        also = (await client.call_tool("preview_table", {"table": "SECRET"})).data
    assert blocked["status"] == "out_of_scope"
    assert "Public.DRIVERS" in blocked["message"]
    assert also["status"] == "out_of_scope"
    mock_client.get.assert_not_called()


async def test_scope_not_enforced_only_filters(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_TABLES": "Public.DRIVERS", "SCOPE_ENFORCE": "false"})
    async with Client(mcp) as client:
        res = (await client.call_tool("describe_table", {"table": "Public.SECRET"})).data
    assert res.get("status") != "out_of_scope"
    assert mock_client.get.called


async def test_list_models_filtered_to_allowlist(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_MODELS": "keep-mod", "ALLOWED_DECISIONS": "missing-dec"})
    mock_client.get.return_value = _make_mock_response(
        {"items": [{"id": "keep-mod", "name": "Keep Me"}, {"id": "drop-mod", "name": "Drop Me"}], "count": 2}
    )
    async with Client(mcp) as client:
        res = (await client.call_tool("list_models", {})).data
    assert [m["id"] for m in res["models"]] == ["keep-mod"]
    assert res["unavailable"] == ["missing-dec"]


async def test_score_data_blocked_when_module_out_of_scope(scoped_server):
    mcp, mock_client = scoped_server({"ALLOWED_DECISIONS": "fraud_decision"})
    route_get(
        mock_client, [("/microanalyticScore/modules/other", _make_mock_response({"id": "other", "name": "Other"}))]
    )
    async with Client(mcp) as client:
        res = (await client.call_tool("score_data", {"model": "other", "input_data": {"x": 1}})).data
    assert res["status"] == "out_of_scope"
    mock_client.post.assert_not_called()


async def test_unscoped_server_allows_everything(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        res = (await client.call_tool("get_use_case", {})).data
        assert res["scoped"] is False
        await client.call_tool("describe_table", {"table": "cas1.Public.anything"})
    assert any("/servers/cas1/caslibs/Public/tables/anything" in c[0][0] for c in mock_client.get.call_args_list)
