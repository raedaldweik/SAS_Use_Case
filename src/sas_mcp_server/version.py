# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The running server's version, for the log line and the MCP handshake."""

from __future__ import annotations

DISTRIBUTION = "sas-mcp-usecase"


def server_version() -> str | None:
    """Return the version of this server.

    A source checkout's ``pyproject.toml`` is authoritative when present (an
    editable install's metadata goes stale); installed deployments — the
    container image — fall through to the distribution metadata.
    """
    try:
        import tomllib
        from pathlib import Path

        # version.py -> src/sas_mcp_server -> src -> repo root
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as fh:
                found = tomllib.load(fh).get("project", {}).get("version")
            if isinstance(found, str) and found:
                return found
    except Exception:  # noqa: BLE001 - fall through to installed metadata
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(DISTRIBUTION)
        except PackageNotFoundError:
            return None
    except Exception:  # noqa: BLE001
        return None
