# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared tool registration for the use-case SAS MCP server.

This server is intentionally scoped to a single use case (one dataset and its
associated models/decisions). It exposes a small, focused tool set — querying
the data, charting it, scoring against ready models, and building new models —
rather than the full SAS Viya surface, so the agent stays a reliable expert on
its dataset instead of being overwhelmed by tools.

All tools are registered via ``register_tools(mcp, get_token)``.
"""

from typing import Optional
from fastmcp import Context
from fastmcp.tools import ToolResult
from .viya_utils import (
    _get_json,
    _get_paged_items,
    _post_json,
    _delete_resource,
    _make_client,
    run_one_snippet,
    run_query_rows,
    fetch_table_columns,
    logger,
)
from .config import MAX_SAS_OUTPUT_CHARS
from .usecase import load_scope


def _truncate_output(text, limit=MAX_SAS_OUTPUT_CHARS):
    """Cap large SAS log/listing text so it can't overflow the agent's context.

    Keeps the head and tail (errors and the final NOTE summary usually sit at
    the end) with a marker noting how much was removed. ``limit`` of 0 disables
    capping.
    """
    if not text or limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n\n...[truncated {omitted} characters to fit the model "
        f"context — re-run a narrower query (fewer columns/rows) for full "
        f"detail]...\n\n{text[-tail:]}"
    )


class ScopeError(Exception):
    """Raised when a tool is asked to act on a resource outside the use-case scope."""


def register_tools(mcp, get_token):
    """Register all tools on *mcp*.

    Parameters
    ----------
    mcp : FastMCP
        The server instance to register tools on.
    get_token : callable
        ``async def get_token(ctx: Context) -> str`` — returns a Viya access
        token.  HTTP mode pulls it from context state; the headless modes
        acquire it via refresh token or password grant.
    """

    # Use-case scope (allowlist) read from environment variables. When no
    # ALLOWED_* variables are set, ``scope.active`` is False and every tool
    # behaves with full access to the environment.
    scope = load_scope()
    if scope.active:
        logger.info(
            "Use-case scope ACTIVE (%s): %d tables, %d models, %d decisions; "
            "primary table=%s; enforce=%s",
            scope.name or "unnamed", len(scope.tables), len(scope.models),
            len(scope.decisions), scope.primary_table, scope.enforce)

    def _guard(allowed: bool, kind: str, value, allowed_list):
        """Block an out-of-scope resource access when enforcement is on."""
        if scope.enforced and not allowed:
            allowed_str = ", ".join(allowed_list) if allowed_list else "(none)"
            raise ScopeError(
                f"'{value}' is outside this assistant's use case and cannot be "
                f"accessed. This assistant is limited to these {kind}: {allowed_str}. "
                f"Call get_use_case to see the full scope."
            )

    # ------------------------------------------------------------------
    # Use-case scope
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_use_case(ctx: Context) -> dict:
        """Return this assistant's use case: the dataset it is an expert on, that table's columns, and the models/decisions it may use.

        Call this first. The data tools default to the primary table reported
        here, so you usually don't need to pass server/caslib/table at all. If
        the assistant is not scoped to a use case, ``scoped`` is false and you
        have full access to the environment.
        """
        logger.info("--- TOOL USED: get_use_case ---")
        manifest = scope.manifest()
        primary = scope.primary_table
        if primary and primary.get("table"):
            info = dict(primary)
            try:
                token = await get_token(ctx)
                async with _make_client(token) as client:
                    info["columns"] = await fetch_table_columns(
                        client, primary["server"], primary["caslib"],
                        primary["table"])
            except Exception as e:  # best-effort grounding — never fail here
                info["columnsNote"] = (
                    f"Columns will be available once the table is queried ({e}).")
            manifest["primaryTable"] = info
        return manifest

    # ------------------------------------------------------------------
    # SAS code execution
    # ------------------------------------------------------------------

    @mcp.tool()
    async def execute_sas_code(sas_code: str, ctx: Context) -> ToolResult:
        """
        Execute SAS code in the Viya environment and return the job's log and listing.

        Use this for anything beyond a simple query: data preparation, PROC-based
        modelling (e.g. PROC LOGISTIC / GRADBOOST / FOREST), assessment, or any
        SAS step. For "show me the top N ..." style questions whose result you
        want to chart, prefer ``query_table`` — it returns clean rows instead of
        log text. Note: each call runs in a fresh compute session that is deleted
        afterwards, so WORK tables do not persist between calls; write anything
        you need to reuse to a caslib.

        Args:
            sas_code (str): the SAS code snippet to execute via the Compute service.

        Returns:
            Structured output with a ``listing`` field (the intended output of the
            code) and a ``log`` field (execution details, including any errors).
        """
        logger.info("--- TOOL USED: execute_sas_code ---")
        token = await get_token(ctx)
        output = await run_one_snippet(sas_code, "1", token)
        # Cap log/listing so a verbose PROC can't blow up the agent's context
        # (output is (snippet_id, state, log, listing)).
        if isinstance(output, (list, tuple)) and len(output) >= 4:
            sid, state, log_text, listing_text = output[:4]
            output = (sid, state,
                      _truncate_output(log_text),
                      _truncate_output(listing_text))
        return output

    @mcp.tool()
    async def query_table(sql: str, ctx: Context, limit: int = 100) -> dict:
        """Run a SQL SELECT against the use-case data and return structured rows (columns + rows).

        This is the tool to use whenever you want to answer a quantitative
        question or feed a chart — e.g. "the top 10 nationalities by count".
        Unlike execute_sas_code (which returns SAS log/listing text), this returns
        clean JSON rows you can pass straight to ``render_chart``.

        Reference the use-case table by its caslib-qualified name from
        get_use_case, for example::

            select Nationality, count(*) as n
            from Public.DRIVER_RISK_SCORE
            group by Nationality
            order by n desc

        Args:
            sql: A single SQL SELECT statement (SAS PROC SQL syntax).
            limit: Maximum rows to return (default 100).
        """
        logger.info("--- TOOL USED: query_table ---")
        token = await get_token(ctx)
        result = await run_query_rows(sql, token, limit=limit)
        if result.get("error"):
            result["log"] = _truncate_output(result.get("log", ""))
        return result

    # ------------------------------------------------------------------
    # Visualization (rendered client-side by the custom UI)
    # ------------------------------------------------------------------

    _CHART_TYPES = ("bar", "line", "area", "pie", "scatter")

    @mcp.tool()
    async def render_chart(chart_type: str, title: str, data: list,
                           x_key: str, y_keys: list, ctx: Context,
                           subtitle: str = "", stacked: bool = False) -> dict:
        """Render an interactive chart in the chat UI.

        Use whenever the user asks to show / plot / visualize / graph / compare
        data, or when a chart makes the answer clearer than text. Call this AFTER
        getting the rows with ``query_table`` (or ``get_castable_data`` for raw
        rows), then pass the rows in as ``data``. Keep ``data`` small — aggregate
        or limit to just the rows you want to chart.

        The chart is drawn by the user interface from this call; the tool itself
        does no plotting and returns the normalized chart spec.

        Args:
            chart_type: One of bar, line, area, pie, scatter.
            title: Chart title.
            data: List of row objects, e.g. [{"month": "Jan", "sales": 120}, ...].
            x_key: Field for the x-axis / category (for pie, the slice label).
            y_keys: Field(s) plotted as series / values (for pie or scatter, one or two).
            subtitle: Optional subtitle.
            stacked: For bar/area, stack the series instead of grouping them.
        """
        logger.info("--- TOOL USED: render_chart ---")
        ct = (chart_type or "").strip().lower()
        if ct not in _CHART_TYPES:
            raise ValueError(
                f"chart_type must be one of {', '.join(_CHART_TYPES)}; got '{chart_type}'.")
        if not isinstance(data, list) or not data:
            raise ValueError("data must be a non-empty list of row objects.")
        if not isinstance(data[0], dict):
            raise ValueError("each item in data must be an object (key/value row).")
        keys = list(data[0].keys())
        missing = [k for k in [x_key, *y_keys] if k not in keys]
        if missing:
            raise ValueError(
                f"these keys are not present in the data rows: {', '.join(missing)}. "
                f"Available keys: {', '.join(keys)}.")
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

    # ------------------------------------------------------------------
    # Data inspection (defaults to the use-case table)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_castable_info(ctx: Context, table_name: Optional[str] = None,
                                caslib_name: Optional[str] = None,
                                server_id: Optional[str] = None) -> dict:
        """Get metadata for the use-case CAS table (row count, column count, size, etc.).

        Call with no arguments to inspect the primary use-case table; pass
        table/caslib/server only to override.
        """
        logger.info("--- TOOL USED: get_castable_info ---")
        server_id, caslib_name, table_name = scope.resolve(
            server_id, caslib_name, table_name)
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                client)

    @mcp.tool()
    async def get_castable_columns(ctx: Context, table_name: Optional[str] = None,
                                   caslib_name: Optional[str] = None,
                                   server_id: Optional[str] = None,
                                   limit: int = 200) -> list:
        """Get column metadata (names, types, labels, formats) for the use-case CAS table.

        Call with no arguments for the primary use-case table; pass
        table/caslib/server only to override.

        Args:
            limit: Maximum columns to return (default 200).
        """
        logger.info("--- TOOL USED: get_castable_columns ---")
        server_id, caslib_name, table_name = scope.resolve(
            server_id, caslib_name, table_name)
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}/columns",
                client, limit=limit)
            return [{"name": c.get("name"), "type": c.get("type"),
                     "rawLength": c.get("rawLength"),
                     "label": c.get("label", ""),
                     "format": c.get("format", "")} for c in items]

    @mcp.tool()
    async def get_castable_data(ctx: Context, table_name: Optional[str] = None,
                                caslib_name: Optional[str] = None,
                                server_id: Optional[str] = None,
                                limit: int = 100, start: int = 0) -> dict:
        """Fetch raw rows from the use-case CAS table with column names.

        Good for sampling the data. For aggregated/filtered results (counts, top
        N, group-by) use ``query_table`` instead. Call with no table arguments
        for the primary use-case table.

        Args:
            limit: Maximum rows to return (default 100).
            start: Row offset (default 0).
        """
        logger.info("--- TOOL USED: get_castable_data ---")
        server_id, caslib_name, table_name = scope.resolve(
            server_id, caslib_name, table_name)
        _guard(scope.allows_table(table_name, caslib_name, server_id),
               "datasets", f"{caslib_name}.{table_name}", scope.tables)
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        data_source_id = f"cas~fs~{server_id}~fs~{caslib_name}"
        table_id = f"cas~fs~{server_id}~fs~{caslib_name}~fs~{table_name}"
        async with _make_client(token) as client:
            columns = []
            col_start = 0
            col_limit = 100
            while True:
                col_resp = await client.get(
                    f"{VIYA_ENDPOINT}/dataTables/dataSources/{data_source_id}/tables/{table_name}/columns",
                    params={"start": col_start, "limit": col_limit},
                    follow_redirects=True,
                )
                col_resp.raise_for_status()
                col_data = col_resp.json()
                for item in col_data.get("items", []):
                    columns.append({"name": item.get("name"), "type": item.get("type"),
                                    "index": item.get("index")})
                total = col_data.get("count", 0)
                col_start += col_limit
                if col_start >= total:
                    break

            row_resp = await client.get(
                f"{VIYA_ENDPOINT}/rowSets/tables/{table_id}/rows",
                params={"start": start, "limit": limit},
                follow_redirects=True,
            )
            row_resp.raise_for_status()
            row_data = row_resp.json()

            col_names = [c["name"] for c in columns]
            rows = []
            for item in row_data.get("items", []):
                cells = item.get("cells", [])
                rows.append(dict(zip(col_names, cells)))

            return {
                "columns": col_names,
                "rows": rows,
                "count": row_data.get("count", len(rows)),
                "start": start,
                "limit": limit,
            }

    # ------------------------------------------------------------------
    # Ready models & decisions — listing and real-time scoring
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_models_and_decisions(ctx: Context, limit: int = 50) -> list:
        """List the ready (published) scoring models and decisions you can score against (MAS modules).

        These are the modules usable with ``score_data`` for real-time scoring.

        Args:
            limit: Maximum modules to return (default 50).
        """
        logger.info("--- TOOL USED: list_models_and_decisions ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/microanalyticScore/modules", client,
                                              limit=limit)
            if scope.active:
                items = [m for m in items
                         if scope.allows_scoreable(m.get("id"), m.get("name"))]
            return [{"id": m.get("id"), "name": m.get("name", ""),
                     "description": m.get("description", "")} for m in items]

    @mcp.tool()
    async def score_data(module_id: str, input_data: dict, ctx: Context,
                         step_id: str = "score") -> dict:
        """Score a record against a ready model or decision (MAS module) in real time.

        Args:
            module_id: MAS module ID (from list_models_and_decisions).
            input_data: Dictionary of input variable name-value pairs.
            step_id: Step within the module (default 'score'; some modules use 'execute').
        """
        logger.info("--- TOOL USED: score_data ---")
        _guard(scope.allows_scoreable(module_id),
               "models or decisions", module_id,
               scope.decisions + scope.models)
        token = await get_token(ctx)
        body = {"inputs": [{"name": k, "value": v} for k, v in input_data.items()]}
        async with _make_client(token) as client:
            return await _post_json(
                f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client,
                body=body)

    # ------------------------------------------------------------------
    # Model building (AutoML pipeline automation)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_ml_projects(ctx: Context, limit: int = 50) -> list:
        """List AutoML pipeline automation projects.

        Args:
            limit: Maximum projects to return (default 50).
        """
        logger.info("--- TOOL USED: list_ml_projects ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                "/mlPipelineAutomation/projects", client, limit=limit)
            return [{"id": p.get("id"), "name": p.get("name", ""),
                     "state": p.get("state", ""),
                     "description": p.get("description", "")} for p in items]

    @mcp.tool()
    async def create_ml_project(project_name: str, data_table_uri: str,
                                target_variable: str, ctx: Context,
                                description: str = "",
                                prediction_type: str = "binary",
                                target_event_level: str = "1",
                                auto_run: bool = True) -> dict:
        """Build a new ML model with AutoML (pipeline automation).

        SAS auto-detects the target's measurement level from the data; for
        classification targets, ``target_event_level`` selects the modeled
        event level. The data table must be loaded in CAS. After it finishes,
        call ``get_ml_project_results`` to see how the model performed.

        Args:
            project_name: Name for the project.
            data_table_uri: URI of the training data table (e.g. '/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ').
            target_variable: Name of the target/response variable.
            description: Optional project description.
            prediction_type: 'binary', 'interval', or 'nominal' (default 'binary'). For 'binary'/'nominal', target_event_level is included.
            target_event_level: Event level for classification targets (default '1'); ignored for 'interval'.
            auto_run: Whether to automatically run pipelines after creation (default True).
        """
        logger.info("--- TOOL USED: create_ml_project ---")
        token = await get_token(ctx)
        # SAS media type required by the MLPA service. Keep
        # analyticsProjectAttributes to the documented, valid fields only —
        # extra attributes (e.g. targetLevel, classSelectionStatistic) make the
        # underlying analytics-project metadata step fail ("...failed to update
        # project metadata. Make sure that the parameters ... are valid").
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        analytics_attrs = {
            "targetVariable": target_variable,
            "partitionEnabled": True,
        }
        if prediction_type in ("binary", "nominal"):
            analytics_attrs["targetEventLevel"] = target_event_level
        body = {
            "name": project_name,
            "description": description,
            "type": "predictive",
            "dataTableUri": data_table_uri,
            "pipelineBuildMethod": "automatic",
            "settings": {
                "autoRun": auto_run,
                "applyGlobalMetadata": False,
                "numberOfModels": 5,
            },
            "analyticsProjectAttributes": analytics_attrs,
        }
        async with _make_client(token) as client:
            return await _post_json("/mlPipelineAutomation/projects", client,
                                    body=body, accept=mlpa_type)

    @mcp.tool()
    async def run_ml_project(project_id: str, ctx: Context) -> dict:
        """Run (train) an AutoML pipeline automation project.

        Args:
            project_id: ID of the project to run.
        """
        logger.info("--- TOOL USED: run_ml_project ---")
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        async with _make_client(token) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                headers={"Accept": mlpa_type},
            )
            get_resp.raise_for_status()
            project_body = get_resp.json()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                params={"action": "retrainProject"},
                content=_json.dumps(project_body).encode(),
                headers={
                    "Content-Type": mlpa_type,
                    "Accept": mlpa_type,
                    "If-Match": etag,
                    "Accept-Language": "en",
                },
            )
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {"status": "running", "projectId": project_id}
            return resp.json()

    @mcp.tool()
    async def get_ml_project_results(project_id: str, ctx: Context) -> dict:
        """Get an AutoML project's results: state, champion model, and the model leaderboard with fit statistics.

        Call after ``run_ml_project`` to report how well the built model
        performs.

        Args:
            project_id: ID of the AutoML project.
        """
        logger.info("--- TOOL USED: get_ml_project_results ---")
        token = await get_token(ctx)
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        async with _make_client(token) as client:
            proj = await _get_json(
                f"/mlPipelineAutomation/projects/{project_id}", client,
                accept=mlpa_type)
            results = {
                "projectId": project_id,
                "name": proj.get("name", ""),
                "state": proj.get("state", ""),
                "championModel": proj.get("championModel"),
            }
            # The candidate-model leaderboard lives on a sub-resource that not
            # every Viya version exposes — best-effort so results never fail.
            try:
                items, _ = await _get_paged_items(
                    f"/mlPipelineAutomation/projects/{project_id}/models",
                    client, limit=50)
                results["leaderboard"] = [
                    {"name": m.get("name", ""),
                     "algorithm": m.get("algorithmName") or m.get("modelType", ""),
                     "champion": m.get("champion", False),
                     "fitStatistics": (m.get("fitStatistics")
                                       or m.get("assessmentStatistics"))}
                    for m in items
                ]
            except Exception as e:
                results["leaderboard"] = []
                results["leaderboardNote"] = f"Leaderboard unavailable: {e}"
            return results

    @mcp.tool()
    async def delete_ml_project(project_id: str, ctx: Context) -> dict:
        """Delete an AutoML pipeline automation project.

        Use this to remove a project (for example, to start over with a
        different configuration).

        Args:
            project_id: ID of the project to delete.
        """
        logger.info("--- TOOL USED: delete_ml_project ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            await _delete_resource(
                f"/mlPipelineAutomation/projects/{project_id}", client)
        return {"status": "deleted", "projectId": project_id}
