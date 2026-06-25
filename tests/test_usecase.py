# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for use-case scoping: the UseCaseScope allowlist logic, the auto-scope
resolution that lets the data tools default to the pinned table, and the
filtering/guard behavior they produce in the registered tools.
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastmcp import FastMCP, Client
from conftest import _make_mock_response

from sas_mcp_server.usecase import UseCaseScope, load_scope, _parse_list


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
    # empty allowlists permit everything
    assert s.allows_report("anything") is True
    assert s.allows_table("anything") is True
    assert s.allows_scoreable("anything") is True


def test_scope_active_when_any_allowlist_set():
    assert UseCaseScope(reports=["r1"]).active is True
    assert UseCaseScope(tables=["t1"]).active is True
    assert UseCaseScope(models=["m1"]).active is True
    assert UseCaseScope(decisions=["d1"]).active is True


def test_report_matching_by_id_or_name_case_insensitive():
    s = UseCaseScope(reports=["RPT-1", "Sales Report"])
    assert s.allows_report("rpt-1")
    assert s.allows_report("zzz", "sales report")  # any candidate matches
    assert not s.allows_report("other-id", "Other Report")


def test_table_matching_supports_qualified_forms():
    s = UseCaseScope(tables=["Public.SALES"])
    assert s.allows_table(name="SALES", caslib="Public")
    assert s.allows_table(name="SALES", caslib="Public", server="cas-shared-default")
    # unqualified allowlist entry matches bare table name
    s2 = UseCaseScope(tables=["SALES"])
    assert s2.allows_table(name="sales", caslib="Public")
    # not in list
    assert not s.allows_table(name="HR", caslib="Public")


def test_allows_scoreable_checks_union_of_models_and_decisions():
    # Only decisions set: a model id not in the decisions list is still blocked.
    s = UseCaseScope(decisions=["fraud_decision"])
    assert s.allows_scoreable("fraud_decision")
    assert not s.allows_scoreable("other_module")
    # Either list can grant access.
    s2 = UseCaseScope(models=["risk_model"], decisions=["fraud_decision"])
    assert s2.allows_scoreable("risk_model")
    assert s2.allows_scoreable("fraud_decision")
    assert not s2.allows_scoreable("nope")
    # Neither set: everything permitted.
    assert UseCaseScope(tables=["t"]).allows_scoreable("anything") is True


def test_enforced_requires_active_and_enforce_flag():
    assert UseCaseScope(reports=["r"], enforce=True).enforced is True
    assert UseCaseScope(reports=["r"], enforce=False).enforced is False
    assert UseCaseScope(enforce=True).enforced is False  # inactive


# ---------------------------------------------------------------------------
# Auto-scope resolution (data tools default to the pinned table)
# ---------------------------------------------------------------------------


def test_parse_table_spec_supports_all_forms():
    s = UseCaseScope(tables=["srv.lib.TBL", "lib2.TBL2", "TBL3"],
                     default_server="cas-shared-default", default_caslib="Public")
    assert s.table_specs[0] == {"server": "srv", "caslib": "lib", "table": "TBL"}
    assert s.table_specs[1] == {"server": "cas-shared-default",
                                "caslib": "lib2", "table": "TBL2"}
    assert s.table_specs[2] == {"server": "cas-shared-default",
                                "caslib": "Public", "table": "TBL3"}


def test_primary_table_is_first_entry():
    s = UseCaseScope(tables=["Public.DRIVERS", "Public.OTHER"])
    assert s.primary_table == {"server": "cas-shared-default",
                               "caslib": "Public", "table": "DRIVERS"}
    assert UseCaseScope().primary_table is None


def test_resolve_fills_missing_from_primary_then_defaults():
    s = UseCaseScope(tables=["myserver.mylib.DRIVERS"])
    # No args → the whole primary table.
    assert s.resolve() == ("myserver", "mylib", "DRIVERS")
    # Explicit table only → caslib/server come from primary.
    assert s.resolve(table="OTHER") == ("myserver", "mylib", "OTHER")
    # Explicit args always win.
    assert s.resolve(server="s2", caslib="c2", table="T2") == ("s2", "c2", "T2")


def test_resolve_uses_defaults_when_no_scope():
    s = UseCaseScope(default_server="cas-shared-default", default_caslib="Public")
    assert s.resolve() == ("cas-shared-default", "Public", None)
    assert s.resolve(table="T") == ("cas-shared-default", "Public", "T")


def test_manifest_contents():
    s = UseCaseScope(name="Fraud", description="d", decisions=["d1"],
                     tables=["Public.DRIVERS"])
    m = s.manifest()
    assert m["useCaseName"] == "Fraud"
    assert m["scoped"] is True
    assert m["enforced"] is True
    assert m["allowedDecisions"] == ["d1"]
    assert m["allowedTables"] == ["Public.DRIVERS"]
    assert m["defaultServer"] == "cas-shared-default"
    assert m["primaryTable"]["table"] == "DRIVERS"


def test_load_scope_reads_env(monkeypatch):
    monkeypatch.setenv("USE_CASE_NAME", "Driver Risk")
    monkeypatch.setenv("ALLOWED_DECISIONS", "risk_decision")
    monkeypatch.setenv("ALLOWED_TABLES", "Public.DRIVER_RISK_SCORE")
    monkeypatch.setenv("DEFAULT_CASLIB", "Public")
    monkeypatch.setenv("SCOPE_ENFORCE", "false")
    s = load_scope()
    assert s.name == "Driver Risk"
    assert s.decisions == ["risk_decision"]
    assert s.tables == ["Public.DRIVER_RISK_SCORE"]
    assert s.active is True
    assert s.enforce is False
    assert s.enforced is False  # active but not enforced
    assert s.primary_table["table"] == "DRIVER_RISK_SCORE"


# ---------------------------------------------------------------------------
# Scoped server behavior (through the MCP protocol)
# ---------------------------------------------------------------------------


def _build_scoped_server(env: dict, monkeypatch):
    """Register tools with use-case env vars set; return (mcp, mock_client, patcher)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get.return_value = _make_mock_response({"items": [], "count": 0})

    patcher = patch("sas_mcp_server.tools._make_client", return_value=mock_client)
    patcher.start()
    mcp = FastMCP("Scoped Test Server")

    async def mock_get_token(ctx):
        return "test-token"

    from sas_mcp_server.tools import register_tools
    register_tools(mcp, mock_get_token)
    return mcp, mock_client, patcher


async def test_get_use_case_returns_manifest(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"USE_CASE_NAME": "Fraud", "ALLOWED_DECISIONS": "keep-mod"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            res = await client.call_tool("get_use_case", {})
        assert res.data["useCaseName"] == "Fraud"
        assert res.data["scoped"] is True
        assert res.data["allowedDecisions"] == ["keep-mod"]
    finally:
        patcher.stop()


async def test_get_use_case_includes_primary_table_columns(monkeypatch):
    mcp, mock_client, patcher = _build_scoped_server(
        {"ALLOWED_TABLES": "Public.DRIVERS"}, monkeypatch)
    try:
        mock_client.get.return_value = _make_mock_response({
            "items": [{"name": "age", "type": "double"},
                      {"name": "nationality", "type": "char"}],
            "count": 2,
        })
        async with Client(mcp) as client:
            res = await client.call_tool("get_use_case", {})
        primary = res.data["primaryTable"]
        assert primary["caslib"] == "Public"
        assert primary["table"] == "DRIVERS"
        col_names = {c["name"] for c in primary["columns"]}
        assert col_names == {"age", "nationality"}
    finally:
        patcher.stop()


async def test_data_tool_defaults_to_primary_table(monkeypatch):
    mcp, mock_client, patcher = _build_scoped_server(
        {"ALLOWED_TABLES": "Public.DRIVER_RISK_SCORE"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            # Called with NO table args — should hit the pinned table.
            await client.call_tool("get_castable_columns", {})
        url = mock_client.get.call_args[0][0]
        assert ("/casManagement/servers/cas-shared-default/caslibs/Public/"
                "tables/DRIVER_RISK_SCORE/columns") in url
    finally:
        patcher.stop()


async def test_get_castable_info_blocked_when_table_out_of_scope(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"ALLOWED_TABLES": "Public.DRIVERS"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            # The pinned table passes the guard (reaches the mocked GET).
            await client.call_tool("get_castable_info", {})
            # A different table is blocked before any HTTP call.
            with pytest.raises(Exception) as ei:
                await client.call_tool("get_castable_info",
                                       {"table_name": "SECRET", "caslib_name": "Public"})
            assert "use case" in str(ei.value).lower()
    finally:
        patcher.stop()


async def test_scope_not_enforced_only_filters(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"ALLOWED_TABLES": "Public.DRIVERS", "SCOPE_ENFORCE": "false"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            # With enforcement off, an out-of-scope table is not blocked.
            res = await client.call_tool(
                "get_castable_info",
                {"table_name": "SECRET", "caslib_name": "Public"})
            assert res.data is not None
    finally:
        patcher.stop()


async def test_list_models_and_decisions_filtered_to_allowlist(monkeypatch):
    mcp, mock_client, patcher = _build_scoped_server(
        {"ALLOWED_DECISIONS": "keep-mod"}, monkeypatch)
    try:
        mock_client.get.return_value = _make_mock_response({
            "items": [
                {"id": "keep-mod", "name": "Keep Me"},
                {"id": "drop-mod", "name": "Drop Me"},
            ],
            "count": 2,
        })
        async with Client(mcp) as client:
            res = await client.call_tool("list_models_and_decisions", {})
        ids = {m["id"] for m in res.data}
        assert ids == {"keep-mod"}
    finally:
        patcher.stop()


async def test_score_data_blocked_when_module_out_of_scope(monkeypatch):
    mcp, _, patcher = _build_scoped_server(
        {"ALLOWED_DECISIONS": "fraud_decision"}, monkeypatch)
    try:
        async with Client(mcp) as client:
            with pytest.raises(Exception) as ei:
                await client.call_tool("score_data", {
                    "module_id": "other_module", "step_id": "execute",
                    "input_data": {"x": 1},
                })
            assert "use case" in str(ei.value).lower()
    finally:
        patcher.stop()


async def test_unscoped_server_allows_everything(monkeypatch):
    # No ALLOWED_* env vars → full access, guards inactive.
    for var in ("ALLOWED_TABLES", "ALLOWED_REPORTS", "ALLOWED_MODELS",
                "ALLOWED_DECISIONS", "USE_CASE_NAME"):
        monkeypatch.delenv(var, raising=False)
    mcp, _, patcher = _build_scoped_server({}, monkeypatch)
    try:
        async with Client(mcp) as client:
            res = await client.call_tool("get_use_case", {})
            assert res.data["scoped"] is False
            # any table is reachable when explicitly named
            await client.call_tool("get_castable_info", {
                "server_id": "cas1", "caslib_name": "Public",
                "table_name": "anything"})
    finally:
        patcher.stop()
