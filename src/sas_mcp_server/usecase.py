# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Use-case scoping for the SAS MCP server.

A "use case" pins the assistant to one dataset (plus any related tables) and
the ready models it may score against, instead of exposing everything in the
SAS Viya environment. The scope is defined entirely through environment
variables, so a non-developer can configure a per-use-case agent — for example
from the SAS Retrieval Agent Manager tool-server Environment Variables tab —
without touching code.

Environment variables
----------------------
``USE_CASE_NAME``         Human-readable name of the use case.
``USE_CASE_DESCRIPTION``  What the assistant is for.
``ALLOWED_TABLES``        Comma/newline-separated CAS tables. Each entry may be
                          ``table``, ``caslib.table``, or
                          ``server.caslib.table``. The first entry is the
                          *primary* table the data tools default to.
``ALLOWED_MODELS``        Comma/newline-separated MAS module ids or names the
                          assistant may score against (published models).
``ALLOWED_DECISIONS``     Same, for published decisions. Kept separate only for
                          readability — both feed one scoring allowlist.
``DEFAULT_CAS_SERVER``    CAS server used when a table entry omits it
                          (default ``cas-shared-default``).
``DEFAULT_CASLIB``        Caslib used when a table entry omits it
                          (default ``Public``).
``SCOPE_ENFORCE``         ``true`` (default) blocks access to out-of-scope
                          resources; ``false`` only hides them from listings.

With none of the ``ALLOWED_*`` variables set, the scope is inactive and the
server has full access to the environment.
"""

import os

DEFAULT_CAS_SERVER = "cas-shared-default"
DEFAULT_CASLIB = "Public"


def _parse_list(raw: str | None) -> list[str]:
    """Split a comma/newline-separated env value into a clean list."""
    if not raw:
        return []
    out = []
    for chunk in raw.replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            out.append(item)
    return out


def _norm(value: object) -> str:
    return str(value).strip().lower()


def parse_table_spec(entry: str, default_server: str, default_caslib: str) -> dict[str, str]:
    """Split ``server.caslib.table`` / ``caslib.table`` / ``table`` into parts."""
    parts = [p.strip().strip('"') for p in str(entry).split(".") if p.strip()]
    if len(parts) >= 3:
        return {"server": parts[0], "caslib": parts[1], "table": parts[2]}
    if len(parts) == 2:
        return {"server": default_server, "caslib": parts[0], "table": parts[1]}
    if len(parts) == 1:
        return {"server": default_server, "caslib": default_caslib, "table": parts[0]}
    return {"server": default_server, "caslib": default_caslib, "table": ""}


class UseCaseScope:
    """An allowlist of the resources a scoped assistant may use."""

    def __init__(
        self,
        name: str = "",
        description: str = "",
        tables=None,
        models=None,
        decisions=None,
        enforce: bool = True,
        default_server: str = DEFAULT_CAS_SERVER,
        default_caslib: str = DEFAULT_CASLIB,
    ) -> None:
        self.name = name
        self.description = description
        self.tables = list(tables or [])
        self.models = list(models or [])
        self.decisions = list(decisions or [])
        self.enforce = enforce
        self.default_server = default_server or DEFAULT_CAS_SERVER
        self.default_caslib = default_caslib or DEFAULT_CASLIB
        self._tables = {_norm(t) for t in self.tables}
        self._scoreables = {_norm(m) for m in self.models} | {_norm(d) for d in self.decisions}
        # Parse each table entry into {server, caslib, table} so the data tools
        # can default to the pinned table without the agent juggling names.
        self.table_specs = [parse_table_spec(t, self.default_server, self.default_caslib) for t in self.tables]

    # -- what is in scope ----------------------------------------------------

    @property
    def active(self) -> bool:
        """True when at least one allowlist is defined."""
        return bool(self._tables or self._scoreables)

    @property
    def enforced(self) -> bool:
        """True when out-of-scope access should be blocked (not just hidden)."""
        return self.active and self.enforce

    @property
    def primary_table(self) -> dict[str, str] | None:
        """The first allowed table as ``{server, caslib, table}`` (or None)."""
        return self.table_specs[0] if self.table_specs else None

    @property
    def scoreables(self) -> list[str]:
        """Every allowed model/decision entry, models first."""
        return self.models + self.decisions

    @property
    def qualified_tables(self) -> list[str]:
        """The allowed tables as ``caslib.table`` names."""
        return [f"{s['caslib']}.{s['table']}" for s in self.table_specs if s["table"]]

    # -- resolution -----------------------------------------------------------

    def resolve(self, server=None, caslib=None, table=None) -> tuple[str, str, str | None]:
        """Fill in missing CAS table coordinates from the primary scoped table.

        Explicit arguments always win; anything left as ``None`` is taken from
        the primary allowed table, then from the configured defaults.
        """
        primary = self.primary_table or {}
        table = table or primary.get("table")
        caslib = caslib or primary.get("caslib") or self.default_caslib
        server = server or primary.get("server") or self.default_server
        return server, caslib, table

    def resolve_spec(self, table: str | None) -> tuple[str, str, str | None]:
        """Resolve one ``[server.][caslib.]table`` string (or None → primary)."""
        if not table or not str(table).strip():
            return self.resolve()
        spec = parse_table_spec(table, "", "")
        return self.resolve(spec["server"] or None, spec["caslib"] or None, spec["table"] or None)

    def find_table(self, name: str) -> dict[str, str] | None:
        """The allowed table spec whose bare name matches *name* (case-insensitive)."""
        wanted = _norm(name)
        for spec in self.table_specs:
            if _norm(spec["table"]) == wanted:
                return spec
        return None

    # -- membership checks (an empty allowlist for a kind permits everything) --

    @staticmethod
    def _match(allowed: set[str], *candidates) -> bool:
        return any(c is not None and _norm(c) in allowed for c in candidates)

    def allows_scoreable(self, *candidates) -> bool:
        """Whether a MAS module (model *or* decision) may be scored.

        When neither allowlist is set every module is permitted; otherwise the
        module's id or name must appear in one of them.
        """
        return not self._scoreables or self._match(self._scoreables, *candidates)

    def allows_table(self, name=None, caslib=None, server=None) -> bool:
        if not self._tables:
            return True
        candidates = [name]
        if caslib and name:
            candidates.append(f"{caslib}.{name}")
        if server and caslib and name:
            candidates.append(f"{server}.{caslib}.{name}")
        return self._match(self._tables, *candidates)

    # -- for the agent --------------------------------------------------------

    def manifest(self) -> dict:
        """A description of the scope suitable for returning to the agent."""
        return {
            "useCaseName": self.name,
            "description": self.description,
            "scoped": self.active,
            "enforced": self.enforced,
            "allowedTables": self.qualified_tables,
            "allowedModels": self.scoreables,
            "defaultServer": self.default_server,
            "defaultCaslib": self.default_caslib,
            "primaryTable": self.primary_table,
        }


def load_scope() -> UseCaseScope:
    """Build a :class:`UseCaseScope` from the current environment variables."""
    enforce = os.getenv("SCOPE_ENFORCE", "true").lower() not in ("false", "0", "no")
    return UseCaseScope(
        name=os.getenv("USE_CASE_NAME", ""),
        description=os.getenv("USE_CASE_DESCRIPTION", ""),
        tables=_parse_list(os.getenv("ALLOWED_TABLES", "")),
        models=_parse_list(os.getenv("ALLOWED_MODELS", "")),
        decisions=_parse_list(os.getenv("ALLOWED_DECISIONS", "")),
        enforce=enforce,
        default_server=os.getenv("DEFAULT_CAS_SERVER", DEFAULT_CAS_SERVER),
        default_caslib=os.getenv("DEFAULT_CASLIB", DEFAULT_CASLIB),
    )
