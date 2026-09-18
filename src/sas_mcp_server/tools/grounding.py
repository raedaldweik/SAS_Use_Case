# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``get_use_case`` — the one call that grounds the agent in its use case."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context, FastMCP

from ..helpers.cas_helpers import fetch_table_columns, fetch_table_info
from ..helpers.mas_helpers import allowed_modules, describe_module
from ..usecase import UseCaseScope
from ._common import make_session_helper

GUIDANCE = [
    "Answer questions about the data with query_data (a FedSQL SELECT; aggregate and filter there, "
    "and keep results small).",
    "describe_table gives the columns and row count; preview_table gives a few raw rows.",
    "To visualise, get the rows with query_data first, then pass them to render_chart.",
    "To score, call describe_model once to see the model's inputs, then score_data with the values "
    "(a query_data row can be passed straight in; unknown keys are ignored).",
]


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]], scope: UseCaseScope) -> None:
    viya_session = make_session_helper(get_token)

    @mcp.tool()
    async def get_use_case(ctx: Context) -> dict[str, Any]:
        """Describe this use case: its dataset (columns, row count) and its ready models (inputs, outputs).

        Call this first in a conversation. The data tools default to the primary
        table reported here, so you rarely need to pass a table name. If the
        assistant is not scoped to a use case, ``scoped`` is false and you have
        access to the whole environment.
        """
        manifest = scope.manifest()
        manifest["guidance"] = list(GUIDANCE)
        async with viya_session("get_use_case", ctx) as (client, _):
            primary = scope.primary_table
            if primary and primary.get("table"):
                info: dict[str, Any] = dict(primary)
                info["qualifiedName"] = f"{primary['caslib']}.{primary['table']}"
                try:
                    meta = await fetch_table_info(client, primary["server"], primary["caslib"], primary["table"])
                    info["rowCount"] = meta.get("rows")
                    info["columnCount"] = meta.get("columns")
                    info["state"] = meta.get("state")
                except Exception as exc:  # best-effort grounding — never fail here
                    info["infoNote"] = f"Table metadata unavailable: {exc}"
                try:
                    info["columns"] = await fetch_table_columns(
                        client, primary["server"], primary["caslib"], primary["table"]
                    )
                except Exception as exc:
                    info["columnsNote"] = f"Columns unavailable until the table can be read: {exc}"
                manifest["primaryTable"] = info
            try:
                listing = await allowed_modules(client, scope)
                models = []
                for module in listing["models"]:
                    try:
                        models.append(await describe_module(client, module))
                    except Exception as exc:
                        models.append({**module, "signatureNote": f"Signature unavailable: {exc}"})
                manifest["models"] = models
                if listing["unavailable"]:
                    manifest["modelsUnavailable"] = listing["unavailable"]
                    manifest["modelsNote"] = (
                        "These allowed models are not published to SAS Micro Analytic Service "
                        "(yet): publish them from SAS Model Manager to score against them."
                    )
            except Exception as exc:
                manifest["modelsNote"] = f"Models unavailable: {exc}"
        return manifest
