# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scoring tools: ``list_models``, ``describe_model`` and ``score_data``."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..config import MAX_SCORE_RECORDS
from ..helpers import mas_helpers
from ..usecase import UseCaseScope
from ..viya_client import post_json
from ._common import coerce_record_or_records, make_session_helper, out_of_scope

Records = Annotated[dict[str, Any] | list[dict[str, Any]], BeforeValidator(coerce_record_or_records)]


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]], scope: UseCaseScope) -> None:
    viya_session = make_session_helper(get_token)
    models_hint = ", ".join(scope.scoreables) if scope.scoreables else "any published model or decision"

    async def _resolve(client, model: str, step_id: str | None) -> tuple[dict | None, dict | None, dict | None]:
        """``(module, step, error)`` for a model name/id and optional step."""
        module = await mas_helpers.resolve_module(client, model)
        if module is None:
            return (
                None,
                None,
                {
                    "status": "not_found",
                    "model": model,
                    "message": (
                        f"No published model or decision named '{model}'. list_models shows the ones "
                        f"available to this assistant; a model must be published to SAS Micro Analytic "
                        f"Service before it can be scored."
                    ),
                },
            )
        if scope.enforced and not scope.allows_scoreable(module["id"], module["name"]):
            return None, None, out_of_scope("models", model, scope.scoreables)
        steps = await mas_helpers.fetch_steps(client, module["id"])
        step = mas_helpers.pick_step(steps, step_id)
        if step is None:
            return (
                module,
                None,
                {
                    "status": "no_step",
                    "model": module["id"],
                    "message": (
                        f"Module '{module['id']}' has no step "
                        + (f"'{step_id}'" if step_id else "to score with")
                        + f". Available steps: {[s.get('id') for s in steps]}."
                    ),
                },
            )
        return module, step, None

    @mcp.tool(
        description=(
            f"List the ready (published) models and decisions this assistant can score against, with "
            f"their ids and names. In scope: {models_hint}. Allowed models that are not published yet "
            f"are reported under 'unavailable'."
        )
    )
    async def list_models(ctx: Context) -> dict[str, Any]:
        async with viya_session("list_models", ctx) as (client, _):
            return await mas_helpers.allowed_modules(client, scope)

    @mcp.tool(
        description=(
            "Describe a ready model or decision: the step it is scored with and that step's typed "
            "input and output variables. Call this before score_data so you pass the right inputs. "
            "'model' is the model's id or name as shown by list_models or get_use_case."
        )
    )
    async def describe_model(model: str, ctx: Context, step_id: str | None = None) -> dict[str, Any]:
        async with viya_session("describe_model", ctx) as (client, _):
            module, step, error = await _resolve(client, model, step_id)
            if error is not None:
                return error
            assert module is not None and step is not None
            steps = await mas_helpers.fetch_steps(client, module["id"])
            return {**module, **mas_helpers.signature(step), "steps": [s.get("id") for s in steps]}

    @mcp.tool(
        description=(
            f"Score one record (or a list of up to {MAX_SCORE_RECORDS} records) against a ready model or "
            f"decision in real time and return its outputs (for example a predicted probability or "
            f"class). 'model' is the id or name from list_models; 'input_data' is an object of input "
            f"variable name → value. Keys are matched to the model's inputs case-insensitively and "
            f"converted to the declared types; keys the model does not use are ignored, and declared "
            f"inputs you omit are scored as missing and listed in 'missingInputs' — so a whole row from "
            f"query_data can be passed as-is. The step (score/execute) is chosen automatically."
        )
    )
    async def score_data(
        model: str,
        input_data: Records,
        ctx: Context,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        records = input_data if isinstance(input_data, list) else [input_data]
        if not records or not all(isinstance(r, dict) for r in records):
            return {
                "status": "invalid_input",
                "message": "input_data must be an object of input values, or a non-empty array of such objects.",
            }
        if len(records) > MAX_SCORE_RECORDS:
            return {
                "status": "invalid_input",
                "message": f"At most {MAX_SCORE_RECORDS} records per call (got {len(records)}). Score in batches.",
            }
        async with viya_session("score_data", ctx) as (client, _):
            module, step, error = await _resolve(client, model, step_id)
            if error is not None:
                return error
            assert module is not None and step is not None
            results = []
            for record in records:
                mapped = mas_helpers.build_inputs(record, step)
                response = await post_json(
                    f"{mas_helpers.MODULES_PATH}/{module['id']}/steps/{step['id']}",
                    client,
                    body={"inputs": mapped["inputs"]},
                )
                results.append(
                    {
                        "outputs": mas_helpers.shape_outputs(response),
                        "inputsUsed": mapped["used"],
                        "ignoredInputs": mapped["ignored"],
                        "missingInputs": mapped["missing"],
                        "executionState": response.get("executionState", ""),
                    }
                )
        base = {"model": module["id"], "modelName": module["name"], "stepId": step.get("id", "")}
        if not isinstance(input_data, list):
            return {**base, **results[0]}
        return {**base, "count": len(results), "results": results}
