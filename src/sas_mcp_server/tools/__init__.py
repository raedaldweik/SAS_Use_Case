# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The use-case tool set.

Eight tools, in three groups, each in its own module exposing
``register(mcp, get_token, scope)``:

* grounding — ``get_use_case``
* data      — ``describe_table``, ``preview_table``, ``query_data``, ``render_chart``
* scoring   — ``list_models``, ``describe_model``, ``score_data``

:func:`register_tools` registers them all on a server, reading the use-case
scope from the environment (see :mod:`sas_mcp_server.usecase`) unless one is
passed in. Every tool is stamped with MCP tool annotations from
:func:`annotations_for` so clients can tell read-only tools apart without any
per-tool code.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from ..usecase import UseCaseScope, load_scope
from ..viya_client import logger
from . import charts, data, grounding, scoring

TOOL_NAMES: tuple[str, ...] = (
    "get_use_case",
    "describe_table",
    "preview_table",
    "query_data",
    "render_chart",
    "list_models",
    "describe_model",
    "score_data",
)

# Tools that neither change server-side state nor leave anything behind.
# query_data runs a job, but only a screened SELECT over CAS tables, where
# Viya authorisation on the caller's identity is the boundary; nothing is
# persisted. score_data invokes a model: harmless, but it is the one tool a
# cautious client may want to confirm, so it is not marked read-only.
READ_ONLY_TOOLS: frozenset[str] = frozenset(TOOL_NAMES) - {"score_data"}


def annotations_for(name: str) -> ToolAnnotations:
    """MCP tool annotations for *name* (hints for the client's approval UX)."""
    read_only = name in READ_ONLY_TOOLS
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


class _Annotator:
    """FastMCP stand-in that stamps annotations on every tool registered through it."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def tool(self, name_or_fn: Any = None, **kwargs: Any) -> Any:
        if callable(name_or_fn):  # bare @mcp.tool
            kwargs.setdefault("annotations", annotations_for(kwargs.get("name") or name_or_fn.__name__))
            return self._target.tool(name_or_fn, **kwargs)

        def decorator(fn: Callable[..., Any]) -> Any:
            explicit = name_or_fn if isinstance(name_or_fn, str) else kwargs.get("name")
            kwargs.setdefault("annotations", annotations_for(explicit or fn.__name__))
            return self._target.tool(name_or_fn, **kwargs)(fn)

        return decorator

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target, item)


def register_tools(
    mcp: FastMCP,
    get_token: Callable[[Context], Awaitable[str]],
    scope: UseCaseScope | None = None,
) -> UseCaseScope:
    """Register the use-case tools on *mcp* and return the scope they use.

    Args:
        mcp: The FastMCP server instance to register tools on.
        get_token: ``async def get_token(ctx: Context) -> str`` returning a
            Viya access token. HTTP mode pulls it from context state; the
            headless modes acquire it with the configured credentials.
        scope: The use-case scope; read from the environment when omitted.
    """
    scope = scope or load_scope()
    if scope.active:
        logger.info(
            "Use-case scope ACTIVE (%s): tables=%s models=%s enforce=%s",
            scope.name or "unnamed",
            scope.qualified_tables,
            scope.scoreables,
            scope.enforce,
        )
    else:
        logger.info("No use-case scope set (no ALLOWED_* variables): full access to the environment")
    target = cast(FastMCP, _Annotator(mcp))
    for module in (grounding, data, scoring, charts):
        module.register(target, get_token, scope)
    return scope


__all__ = ["READ_ONLY_TOOLS", "TOOL_NAMES", "annotations_for", "register_tools"]
