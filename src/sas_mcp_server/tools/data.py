# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Data tools: ``describe_table``, ``preview_table`` and ``query_data``."""

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..helpers import fedsql_helpers
from ..helpers.cas_helpers import (
    fetch_table_columns,
    fetch_table_info,
    fetch_table_rows,
    is_not_found,
    not_found_message,
)
from ..usecase import UseCaseScope
from ..viya_client import get_paged_items
from ..viya_utils import locked_session, run_job, submit_job
from ._common import clamp, make_session_helper, out_of_scope

MAX_PREVIEW_ROWS = 1000


def _not_found(caslib: str, name: str) -> dict[str, Any]:
    return {"status": "not_found", "table": f"{caslib}.{name}", "message": not_found_message(caslib, name)}


def scope_query(query: str, scope: UseCaseScope) -> tuple[str, dict[str, Any] | None]:
    """Qualify bare table names and refuse out-of-scope ones.

    A bare name that matches an allowed table (``from PATIENTS``) is rewritten
    to its caslib-qualified form, so the agent need not remember the caslib.
    A qualified name outside the allowlist is refused when the scope is
    enforced. Other bare names are left alone: they may be a function's
    ``FROM`` (``extract(year from d)``) and FedSQL reports them if not.
    """
    replacements: list[tuple[int, int, str]] = []
    for ref in fedsql_helpers.find_table_refs(query):
        parts = ref["parts"]
        if len(parts) == 1:
            spec = scope.find_table(parts[0])
            if spec:
                replacements.append((ref["start"], ref["end"], f"{spec['caslib']}.{spec['table']}"))
            continue
        caslib, table = parts[-2], parts[-1]
        if scope.enforced and not scope.allows_table(table, caslib):
            return query, out_of_scope("tables", f"{caslib}.{table}", scope.qualified_tables)
    return fedsql_helpers.replace_spans(query, replacements), None


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]], scope: UseCaseScope) -> None:
    viya_session = make_session_helper(get_token)
    primary = scope.primary_table
    primary_name = f"{primary['caslib']}.{primary['table']}" if primary and primary.get("table") else None
    tables_hint = ", ".join(scope.qualified_tables) if scope.qualified_tables else "any CAS table"
    default_hint = (
        f"Call with no table for the use-case table {primary_name}."
        if primary_name
        else "Pass table as caslib.table (for example Public.HMEQ)."
    )

    def _resolve(table: str | None) -> tuple[str, str, str, dict[str, Any] | None]:
        server, caslib, name = scope.resolve_spec(table)
        if not name:
            return (
                server,
                caslib,
                "",
                {
                    "status": "no_table",
                    "message": "No table given and no ALLOWED_TABLES configured. Pass table='caslib.table'.",
                },
            )
        if scope.enforced and not scope.allows_table(name, caslib, server):
            return server, caslib, name, out_of_scope("tables", f"{caslib}.{name}", scope.qualified_tables)
        return server, caslib, name, None

    @mcp.tool(
        description=(
            f"Describe a CAS table: row count, column count and every column's name, type, label and "
            f"format. {default_hint} Tables in scope: {tables_hint}."
        )
    )
    async def describe_table(ctx: Context, table: str | None = None) -> dict[str, Any]:
        server, caslib, name, error = _resolve(table)
        if error:
            return error
        async with viya_session("describe_table", ctx) as (client, _):
            try:
                info = await fetch_table_info(client, server, caslib, name)
                columns = await fetch_table_columns(client, server, caslib, name)
            except httpx.HTTPStatusError as exc:
                if is_not_found(exc):
                    return _not_found(caslib, name)
                raise
        return {
            "table": f"{caslib}.{name}",
            "server": server,
            "caslib": caslib,
            "name": name,
            "rowCount": info.get("rows"),
            "columnCount": info.get("columns", len(columns)),
            "state": info.get("state", ""),
            "columns": columns,
        }

    @mcp.tool(
        description=(
            f"Fetch a page of raw rows from a CAS table, values formatted as SAS displays them. Good for a "
            f"quick look at the data; for filtered, aggregated or sorted results use query_data. "
            f"{default_hint} limit is 1..{MAX_PREVIEW_ROWS} (default 20); start pages through the table."
        )
    )
    async def preview_table(ctx: Context, table: str | None = None, limit: int = 20, start: int = 0) -> dict[str, Any]:
        server, caslib, name, error = _resolve(table)
        if error:
            return error
        limit = clamp(limit, 1, MAX_PREVIEW_ROWS)
        start = max(0, int(start))
        async with viya_session("preview_table", ctx) as (client, _):
            try:
                result = await fetch_table_rows(client, server, caslib, name, limit=limit, start=start)
            except httpx.HTTPStatusError as exc:
                if is_not_found(exc):
                    return _not_found(caslib, name)
                raise
        result["table"] = f"{caslib}.{name}"
        return result

    query_target = f"the use-case table {primary_name}" if primary_name else "CAS tables (qualify as caslib.table)"

    @mcp.tool(
        description=(
            f"Run a FedSQL SELECT against {query_target} and get the rows back as JSON. This is the tool for "
            f"any question about the data: counts, averages, group-bys, top-N, filters, joins between the "
            f"tables in scope ({tables_hint}). A bare table name is qualified for you. "
            f"Rows are capped by limit (1..{fedsql_helpers.MAX_LIMIT}, default 100) — any LIMIT you write is "
            f"ignored — so aggregate in SQL rather than pulling raw rows; add ORDER BY for stable paging "
            f"with start. Dialect: FedSQL (no WITH/CTE — use a derived table; double-quote identifiers "
            f"that are reserved words or contain spaces; use single quotes for string literals). Only a "
            f"single read-only SELECT is accepted. On failure a status dict names the fix."
        )
    )
    async def query_data(query: str, ctx: Context, limit: int = 100, start: int = 0) -> dict[str, Any]:
        error = fedsql_helpers.screen_query(query, limit, start)
        if error is not None:
            return error
        query, error = scope_query(query, scope)
        if error is not None:
            return error

        uid = uuid.uuid4().hex[:8]
        code = fedsql_helpers.build_query_code(query, limit=limit, start=start, uid=uid)
        async with viya_session("query_data", ctx) as (client, token), locked_session(client, token) as session_id:
            # poll=0.5: the 2s default dominates the ~0.6s a small query takes.
            state, log, _ = await run_job(client, session_id, code, poll=0.5)
            try:
                # A failed FedSQL step can still report state 'completed', so the
                # log is the authority on success — never the state alone.
                mapped = fedsql_helpers.map_error(log)
                if mapped is not None:
                    return mapped
                if state != "completed":
                    return {
                        "status": "query_failed",
                        "message": f"The query ended in state '{state}' without a reported error.",
                        "sas_errors": [],
                    }
                col_items, _ = await get_paged_items(
                    f"/compute/sessions/{session_id}/data/WORK/_Q{uid}/columns", client, limit=1000
                )
                columns = fedsql_helpers.describe_columns(col_items)
                # Rows come from the format-stripped twin so numerics stay numeric
                # and missings stay null; one row past the page proves truncation.
                row_items, _ = await get_paged_items(
                    f"/compute/sessions/{session_id}/data/WORK/_F{uid}/rows",
                    client,
                    limit=limit + 1,
                    start=start,
                )
                truncated = len(row_items) > limit
                rows = fedsql_helpers.convert_rows(row_items[:limit], columns)
                return {
                    "columns": [c["name"] for c in columns],
                    "rows": rows,
                    "count": len(rows),
                    "start": start,
                    "limit": limit,
                    "truncated": truncated,
                    "column_types": {c["name"]: c["type"] for c in columns},
                    "query": query,
                }
            finally:
                # Best effort: WORK dies with the session anyway, but a long-lived
                # warm session would otherwise accumulate scratch tables.
                with contextlib.suppress(Exception):
                    await submit_job(client, session_id, fedsql_helpers.build_cleanup_code(uid))
