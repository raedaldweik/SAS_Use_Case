# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Use-case scoping for the SAS MCP server.

A "use case" restricts the assistant to a curated subset of the SAS Viya
environment — specific CAS tables, models, and decisions — instead of exposing
everything. The scope is defined entirely through environment variables, so a
non-developer can configure a per-use-case chatbot (for example, from the SAS
Retrieval Agent Manager tool-server Environment Variables tab) without touching
code.

Environment variables
----------------------
``USE_CASE_NAME``         Human-readable name of the use case.
``USE_CASE_DESCRIPTION``  What the assistant is for.
``ALLOWED_TABLES``        Comma/newline-separated CAS tables. Each entry may be
                          ``table``, ``caslib.table``, or
                          ``server.caslib.table``. The first entry is treated as
                          the *primary* table and is what the data tools default
                          to when called without an explicit table.
``ALLOWED_REPORTS``       Comma/newline-separated report IDs or names.
``ALLOWED_MODELS``        Comma/newline-separated model IDs or names.
``ALLOWED_DECISIONS``     Comma/newline-separated decision/MAS-module IDs or names.
``DEFAULT_CAS_SERVER``    CAS server used when a table entry omits the server
                          (default ``cas-shared-default``).
``DEFAULT_CASLIB``        Caslib used when a table entry omits the caslib
                          (default ``Public``).
``SCOPE_ENFORCE``         ``true`` (default) blocks access to out-of-scope
                          resources; ``false`` only hides them from listings.

If none of the ``ALLOWED_*`` variables are set, the scope is inactive and the
server behaves exactly as before — full access to the environment.
"""

import os
from typing import Optional

DEFAULT_CAS_SERVER = "cas-shared-default"
DEFAULT_CASLIB = "Public"


def _parse_list(raw: Optional[str]) -> list:
    """Split a comma/newline-separated env value into a clean list."""
    if not raw:
        return []
    out = []
    for chunk in raw.replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            out.append(item)
    return out


def _norm(value) -> str:
    return str(value).strip().lower()


class UseCaseScope:
    """An allowlist of the resources a scoped assistant may use."""

    def __init__(self, name="", description="", tables=None, reports=None,
                 models=None, decisions=None, enforce=True,
                 default_server=DEFAULT_CAS_SERVER,
                 default_caslib=DEFAULT_CASLIB):
        self.name = name
        self.description = description
        self.tables = list(tables or [])
        self.reports = list(reports or [])
        self.models = list(models or [])
        self.decisions = list(decisions or [])
        self.enforce = enforce
        self.default_server = default_server or DEFAULT_CAS_SERVER
        self.default_caslib = default_caslib or DEFAULT_CASLIB
        self._tables = {_norm(t) for t in self.tables}
        self._reports = {_norm(r) for r in self.reports}
        self._models = {_norm(m) for m in self.models}
        self._decisions = {_norm(d) for d in self.decisions}
        # Parse each table entry into {server, caslib, table} so the data tools
        # can default to the pinned table without the agent juggling identifiers.
        self.table_specs = [
            self._parse_table_spec(t, self.default_server, self.default_caslib)
            for t in self.tables
        ]

    @staticmethod
    def _parse_table_spec(entry, default_server, default_caslib) -> dict:
        """Split ``server.caslib.table`` / ``caslib.table`` / ``table`` into parts."""
        parts = [p.strip() for p in str(entry).split(".") if p.strip()]
        if len(parts) >= 3:
            return {"server": parts[0], "caslib": parts[1], "table": parts[2]}
        if len(parts) == 2:
            return {"server": default_server, "caslib": parts[0], "table": parts[1]}
        if len(parts) == 1:
            return {"server": default_server, "caslib": default_caslib,
                    "table": parts[0]}
        return {"server": default_server, "caslib": default_caslib, "table": ""}

    @property
    def active(self) -> bool:
        """True when at least one allowlist is defined."""
        return bool(self._tables or self._reports or self._models
                    or self._decisions)

    @property
    def enforced(self) -> bool:
        """True when out-of-scope access should be blocked (not just hidden)."""
        return self.active and self.enforce

    @property
    def primary_table(self) -> Optional[dict]:
        """The first allowed table as ``{server, caslib, table}`` (or None)."""
        return self.table_specs[0] if self.table_specs else None

    def resolve(self, server=None, caslib=None, table=None):
        """Fill in missing CAS table coordinates from the primary scoped table.

        Explicit arguments always win; anything left as ``None`` is taken from
        the primary allowed table, then from the configured defaults. This lets
        the data tools be called with no arguments and still act on the pinned
        use-case table.
        """
        primary = self.primary_table or {}
        table = table or primary.get("table")
        caslib = caslib or primary.get("caslib") or self.default_caslib
        server = server or primary.get("server") or self.default_server
        return server, caslib, table

    @staticmethod
    def _match(allowed: set, *candidates) -> bool:
        return any(c is not None and _norm(c) in allowed for c in candidates)

    # -- membership checks (an empty allowlist for a kind permits everything) --

    def allows_report(self, *candidates) -> bool:
        return not self._reports or self._match(self._reports, *candidates)

    def allows_model(self, *candidates) -> bool:
        return not self._models or self._match(self._models, *candidates)

    def allows_decision(self, *candidates) -> bool:
        return not self._decisions or self._match(self._decisions, *candidates)

    def allows_scoreable(self, *candidates) -> bool:
        """Whether a MAS module (model *or* decision) may be scored.

        Checks the union of the models and decisions allowlists. When neither is
        set, every module is permitted; otherwise a module must appear in one of
        them (so setting only ALLOWED_DECISIONS still restricts scoring).
        """
        combined = self._models | self._decisions
        return not combined or self._match(combined, *candidates)

    def allows_table(self, name=None, caslib=None, server=None) -> bool:
        if not self._tables:
            return True
        candidates = [name]
        if caslib and name:
            candidates.append(f"{caslib}.{name}")
        if server and caslib and name:
            candidates.append(f"{server}.{caslib}.{name}")
        return self._match(self._tables, *candidates)

    def manifest(self) -> dict:
        """A description of the scope suitable for returning to the agent."""
        return {
            "useCaseName": self.name,
            "description": self.description,
            "scoped": self.active,
            "enforced": self.enforced,
            "allowedTables": self.tables,
            "allowedReports": self.reports,
            "allowedModels": self.models,
            "allowedDecisions": self.decisions,
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
        reports=_parse_list(os.getenv("ALLOWED_REPORTS", "")),
        models=_parse_list(os.getenv("ALLOWED_MODELS", "")),
        decisions=_parse_list(os.getenv("ALLOWED_DECISIONS", "")),
        enforce=enforce,
        default_server=os.getenv("DEFAULT_CAS_SERVER", DEFAULT_CAS_SERVER),
        default_caslib=os.getenv("DEFAULT_CASLIB", DEFAULT_CASLIB),
    )
