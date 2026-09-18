# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Side-effect-free environment-variable helpers shared across modules."""

import os

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def env_bool(name: str, default: bool) -> bool:
    """Parse a boolean-ish environment variable.

    Recognises ``true/1/yes/on`` and ``false/0/no/off`` (case-insensitive).
    Returns *default* when the variable is unset or holds an unrecognised value.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return default


def env_int(name: str, default: int) -> int:
    """Parse an integer environment variable, falling back to *default*."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default
