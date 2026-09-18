# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read a CAS table's metadata, columns and rows through the Viya REST APIs."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import VIYA_ENDPOINT
from ..viya_client import get_json, get_paged_items, raise_for_viya_status

COLUMN_FIELDS = ("name", "type", "rawLength", "label", "format")


def qualified_name(caslib: str, table: str) -> str:
    return f"{caslib}.{table}"


def not_found_message(caslib: str, table: str) -> str:
    """Why casManagement has no loaded table, in the two usual cases."""
    return (
        f"CAS has no loaded table '{table}' in caslib '{caslib}'. Two usual causes: "
        f"(1) the table exists on disk but is not loaded into memory — load it "
        f"(for example in SAS Visual Analytics or Manage Data, or with PROC CASUTIL "
        f"LOAD ... PROMOTE); (2) it was created in a session without PROMOTE=YES, "
        f"so it is session-scoped and invisible to other sessions — re-create it "
        f"promoted. Check the spelling of the caslib and table as well."
    )


def is_not_found(exc: httpx.HTTPStatusError) -> bool:
    return exc.response is not None and exc.response.status_code == 404


async def fetch_table_info(client: httpx.AsyncClient, server: str, caslib: str, table: str) -> dict[str, Any]:
    """The casManagement table document (row count, column count, state, ...)."""
    return await get_json(f"/casManagement/servers/{server}/caslibs/{caslib}/tables/{table}", client)


async def fetch_table_columns(
    client: httpx.AsyncClient, server: str, caslib: str, table: str, limit: int = 500
) -> list[dict[str, Any]]:
    """Column metadata (name, type, length, label, format) for a CAS table."""
    items, _ = await get_paged_items(
        f"/casManagement/servers/{server}/caslibs/{caslib}/tables/{table}/columns",
        client,
        limit=limit,
    )
    return [{field: c.get(field, "") for field in COLUMN_FIELDS} for c in items]


async def fetch_table_rows(
    client: httpx.AsyncClient,
    server: str,
    caslib: str,
    table: str,
    limit: int = 100,
    start: int = 0,
) -> dict[str, Any]:
    """A page of rows from a CAS table, as dicts keyed by column name.

    Column names come from the dataTables service (paged), the rows from the
    rowSets service. Values arrive formatted the way SAS displays them.
    """
    data_source_id = f"cas~fs~{server}~fs~{caslib}"
    table_id = f"cas~fs~{server}~fs~{caslib}~fs~{table}"
    columns: list[str] = []
    col_start = 0
    col_limit = 100
    while True:
        col_resp = await client.get(
            f"{VIYA_ENDPOINT}/dataTables/dataSources/{data_source_id}/tables/{table}/columns",
            params={"start": col_start, "limit": col_limit},
            follow_redirects=True,
        )
        raise_for_viya_status(col_resp)
        col_data = col_resp.json()
        columns.extend(item.get("name") for item in col_data.get("items", []))
        total = col_data.get("count", 0)
        col_start += col_limit
        if col_start >= total:
            break

    row_resp = await client.get(
        f"{VIYA_ENDPOINT}/rowSets/tables/{table_id}/rows",
        params={"start": start, "limit": limit},
        follow_redirects=True,
    )
    raise_for_viya_status(row_resp)
    row_data = row_resp.json()
    rows = [dict(zip(columns, item.get("cells", []), strict=False)) for item in row_data.get("items", [])]
    total_rows = row_data.get("count", len(rows))
    return {
        "columns": columns,
        "rows": rows,
        "count": total_rows,
        "start": start,
        "limit": limit,
        "truncated": start + len(rows) < total_rows,
    }
