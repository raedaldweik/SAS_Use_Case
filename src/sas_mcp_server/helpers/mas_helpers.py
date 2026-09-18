# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Real-time scoring against SAS Micro Analytic Service (MAS) modules.

A model published from SAS Model Manager / Model Studio (or a decision from
SAS Intelligent Decisioning) becomes a MAS *module* with one or more *steps*.
Each step declares typed inputs and outputs; scoring is a POST of named input
values to a step. These helpers hide the module-id/step/type plumbing so the
tools can take a model *name*, a plain record, and return a plain result:

* :func:`resolve_module` — the module for an id **or** a display name.
* :func:`fetch_steps` / :func:`pick_step` — the step to score with (``score``
  for models, ``execute`` for decisions) and its signature.
* :func:`build_inputs` — match a record's keys to the declared inputs
  case-insensitively, coerce types, and report what was ignored or missing.

Module and step lookups are cached for the process lifetime: signatures do not
change once published, and one chat turn may score several records.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..viya_client import get_json, get_paged_items

MODULES_PATH = "/microanalyticScore/modules"
STEP_PREFERENCE = ("score", "execute")

_INTEGER_TYPES = frozenset({"integer", "int", "bigint", "long"})
_NUMERIC_TYPES = _INTEGER_TYPES | frozenset({"decimal", "double", "float", "number"})
_STRING_TYPES = frozenset({"string", "char", "varchar"})

_module_cache: dict[str, dict[str, Any]] = {}
_steps_cache: dict[str, list[dict[str, Any]]] = {}


def clear_cache() -> None:
    """Forget cached modules and steps (test isolation / republished model)."""
    _module_cache.clear()
    _steps_cache.clear()


def module_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "description": item.get("description", ""),
    }


def _remember(module: dict[str, Any]) -> dict[str, Any]:
    for key in (module.get("id"), module.get("name")):
        if key:
            _module_cache[str(key).strip().lower()] = module
    return module


async def list_modules(client: httpx.AsyncClient, limit: int = 500) -> list[dict[str, Any]]:
    """Every published MAS module as ``{id, name, description}``."""
    items, _ = await get_paged_items(MODULES_PATH, client, limit=limit)
    return [_remember(module_summary(m)) for m in items]


async def resolve_module(client: httpx.AsyncClient, model: str) -> dict[str, Any] | None:
    """Find the MAS module for *model*, an id or a display name (case-insensitive).

    Module ids are usually the lower-cased name, so an exact GET is tried
    first; otherwise the module list is searched by id and name.
    """
    key = (model or "").strip().lower()
    if not key:
        return None
    cached = _module_cache.get(key)
    if cached is not None:
        return cached
    try:
        item = await get_json(f"{MODULES_PATH}/{key}", client)
        return _remember(module_summary(item))
    except httpx.HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code not in (400, 404):
            raise
    for module in await list_modules(client):
        if module["id"].lower() == key or module["name"].lower() == key:
            return module
    return None


async def fetch_steps(client: httpx.AsyncClient, module_id: str) -> list[dict[str, Any]]:
    """The module's steps, each with its declared ``inputs`` and ``outputs``."""
    cached = _steps_cache.get(module_id)
    if cached is not None:
        return cached
    items, _ = await get_paged_items(f"{MODULES_PATH}/{module_id}/steps", client, limit=50)
    _steps_cache[module_id] = items
    return items


def pick_step(steps: list[dict[str, Any]], step_id: str | None = None) -> dict[str, Any] | None:
    """Choose the step to score with: the requested one, else score/execute/first."""
    if step_id:
        wanted = step_id.strip().lower()
        for step in steps:
            if str(step.get("id", "")).lower() == wanted:
                return step
        return None
    for preferred in STEP_PREFERENCE:
        for step in steps:
            if str(step.get("id", "")).lower() == preferred:
                return step
    return steps[0] if steps else None


def _variables(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        var: dict[str, Any] = {"name": item["name"], "type": item.get("type", "")}
        if item.get("dim"):
            var["dim"] = item["dim"]
        out.append(var)
    return out


def signature(step: dict[str, Any]) -> dict[str, Any]:
    """The step's id and its typed inputs and outputs."""
    return {
        "stepId": step.get("id", ""),
        "inputs": _variables(step.get("inputs")),
        "outputs": _variables(step.get("outputs")),
    }


def coerce_value(value: Any, type_name: str | None) -> Any:
    """Coerce a record value to the declared MAS type; leave it alone if that fails."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    t = (type_name or "").strip().lower()
    try:
        if t in _INTEGER_TYPES:
            return int(float(value))
        if t in _NUMERIC_TYPES:
            return float(value)
        if t in _STRING_TYPES:
            return str(value)
    except (TypeError, ValueError):
        return value
    return value


def build_inputs(record: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """Map *record* onto the step's declared inputs.

    Keys match declared input names case-insensitively, values are coerced to
    the declared type, keys the step does not declare are dropped (a whole
    table row can be passed straight in), and declared inputs the record did
    not supply are reported so the caller can see what scored as missing. A
    step that declares no inputs is passed the record unchanged.
    """
    declared = [d for d in (step.get("inputs") or []) if isinstance(d, dict) and d.get("name")]
    if not declared:
        inputs = [{"name": str(k), "value": v} for k, v in record.items()]
        return {"inputs": inputs, "used": dict(record), "ignored": [], "missing": []}
    by_lower = {str(d["name"]).lower(): d for d in declared}
    inputs: list[dict[str, Any]] = []
    used: dict[str, Any] = {}
    ignored: list[str] = []
    for key, value in record.items():
        decl = by_lower.get(str(key).strip().lower())
        if decl is None:
            ignored.append(str(key))
            continue
        coerced = coerce_value(value, decl.get("type"))
        inputs.append({"name": decl["name"], "value": coerced})
        used[decl["name"]] = coerced
    missing = [d["name"] for d in declared if d["name"] not in used]
    return {"inputs": inputs, "used": used, "ignored": ignored, "missing": missing}


def shape_outputs(response: dict[str, Any]) -> dict[str, Any]:
    """The step response's outputs as ``{name: value}``."""
    outputs = response.get("outputs") or []
    return {o.get("name"): o.get("value") for o in outputs if isinstance(o, dict) and o.get("name")}


# --- scope-aware listing -----------------------------------------------------


async def allowed_modules(client: httpx.AsyncClient, scope) -> dict[str, Any]:
    """The published modules the scope permits, and allowed entries not (yet) published.

    ``unavailable`` names the ``ALLOWED_MODELS`` entries with no matching MAS
    module — the usual reason a bootcamp model "does not work": it has not
    been published to SAS Micro Analytic Service yet.
    """
    modules = await list_modules(client)
    allowed = [m for m in modules if scope.allows_scoreable(m["id"], m["name"])]
    matched = {m["id"].lower() for m in allowed} | {m["name"].lower() for m in allowed}
    unavailable = [entry for entry in scope.scoreables if entry.strip().lower() not in matched]
    return {"models": allowed, "unavailable": unavailable}


async def describe_module(client: httpx.AsyncClient, module: dict[str, Any]) -> dict[str, Any]:
    """The module summary plus the signature of the step it is scored with."""
    steps = await fetch_steps(client, module["id"])
    step = pick_step(steps)
    out = dict(module)
    out["steps"] = [s.get("id") for s in steps]
    if step is not None:
        out.update(signature(step))
    return out
