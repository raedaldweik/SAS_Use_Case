# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``render_chart`` — an interactive chart spec for the chat UI to draw."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..config import MAX_CHART_ROWS
from ..usecase import UseCaseScope
from ..viya_client import logger
from ._common import coerce_json_list, coerce_str_or_json_list

CHART_TYPES = ("bar", "line", "area", "pie", "scatter")

Rows = Annotated[list[dict[str, Any]], BeforeValidator(coerce_json_list)]
Keys = Annotated[list[str], BeforeValidator(coerce_str_or_json_list)]


def build_chart_spec(
    chart_type: str,
    title: str,
    data: list[dict[str, Any]],
    x_key: str,
    y_keys: list[str],
    subtitle: str = "",
    stacked: bool = False,
) -> dict[str, Any]:
    """Validate the arguments and return the ``kind: "chart"`` spec."""
    ct = (chart_type or "").strip().lower()
    if ct not in CHART_TYPES:
        raise ValueError(f"chart_type must be one of {', '.join(CHART_TYPES)}; got '{chart_type}'.")
    if not isinstance(data, list) or not data:
        raise ValueError("data must be a non-empty list of row objects.")
    if not all(isinstance(row, dict) for row in data):
        raise ValueError("each item in data must be an object (key/value row).")
    if len(data) > MAX_CHART_ROWS:
        raise ValueError(
            f"data has {len(data)} rows; at most {MAX_CHART_ROWS} can be charted. Aggregate or limit the query first."
        )
    if not y_keys:
        raise ValueError("y_keys must list at least one field to plot.")
    keys = list(data[0].keys())
    missing = [k for k in [x_key, *y_keys] if k not in keys]
    if missing:
        raise ValueError(
            f"these keys are not present in the data rows: {', '.join(missing)}. Available keys: {', '.join(keys)}."
        )
    return {
        "kind": "chart",
        "type": ct,
        "title": title,
        "subtitle": subtitle,
        "data": data,
        "xKey": x_key,
        "yKeys": list(y_keys),
        "stacked": bool(stacked),
    }


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]], scope: UseCaseScope) -> None:
    @mcp.tool()
    async def render_chart(
        chart_type: str,
        title: str,
        data: Rows,
        x_key: str,
        y_keys: Keys,
        ctx: Context,
        subtitle: str = "",
        stacked: bool = False,
    ) -> dict[str, Any]:
        """Render an interactive chart in the chat UI from rows you already have.

        Use whenever the user asks to show / plot / visualise / compare data, or
        when a chart makes the answer clearer than text. Get the rows first with
        ``query_data`` (aggregate there — e.g. counts per category, averages per
        month — and keep it to the rows you want to chart, such as the top 10),
        then pass them in as ``data``.

        The chart is drawn by the chat front-end from the spec this returns
        (tagged ``kind: "chart"``); the tool itself does no plotting.

        Args:
            chart_type: One of bar, line, area, pie, scatter.
            title: Chart title.
            data: List of row objects, each keyed by column name, exactly as query_data returns them.
            x_key: Field for the x-axis / category (for pie, the slice label).
            y_keys: Field(s) plotted as series / values (for pie or scatter, one or two).
            subtitle: Optional subtitle.
            stacked: For bar/area, stack the series instead of grouping them.
        """
        logger.info("--- TOOL USED: render_chart ---")
        return build_chart_spec(chart_type, title, data, x_key, y_keys, subtitle=subtitle, stacked=stacked)
