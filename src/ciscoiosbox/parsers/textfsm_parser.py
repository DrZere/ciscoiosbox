"""Thin wrapper around ntc-templates / TextFSM.

Strategy: try the community TextFSM template first, because it is maintained
against many IOS versions. When no template exists, the template fails to
match, or ntc-templates is not installed, fall back to the hand-written regex
parsers in the sibling modules.

This keeps the app working in a stripped-down frozen build while still getting
the benefit of ntc-templates when it is present.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_TEMPLATES_AVAILABLE: bool | None = None


def templates_available() -> bool:
    """True when ntc-templates can be imported. Result is cached."""
    global _TEMPLATES_AVAILABLE
    if _TEMPLATES_AVAILABLE is None:
        try:
            from ntc_templates.parse import parse_output  # noqa: F401
            _TEMPLATES_AVAILABLE = True
        except Exception:  # noqa: BLE001
            log.info("ntc-templates unavailable; using built-in regex parsers.")
            _TEMPLATES_AVAILABLE = False
    return _TEMPLATES_AVAILABLE


def parse(command: str, output: str, platform: str = "cisco_ios") -> list[dict[str, Any]] | None:
    """Parse ``output`` with the ntc-template for ``command``.

    Returns a list of row dicts (keys lower-cased), or ``None`` when the
    template is missing or produced nothing — the caller should then use its
    regex fallback.
    """
    if not output or not output.strip():
        return None
    if not templates_available():
        return None

    from ntc_templates.parse import parse_output

    try:
        rows = parse_output(platform=platform, command=command, data=output)
    except Exception as exc:  # noqa: BLE001 - a missing template raises plain Exception
        log.debug("No usable template for '%s' on %s: %s", command, platform, exc)
        return None

    if not rows:
        # An empty result is ambiguous: it can mean "template matched nothing"
        # or "device genuinely has no rows". Treat it as a miss and let the
        # regex fallback decide, since a false empty grid is worse than a retry.
        return None

    return [{str(k).lower(): v for k, v in row.items()} for row in rows]


def first(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Return the first row, or an empty dict."""
    return rows[0] if rows else {}


def get(row: dict[str, Any], *keys: str, default: str = "") -> str:
    """Fetch the first present key, as a stripped string.

    ntc-templates renames fields between releases (``interface`` vs ``intf``),
    so every lookup lists the aliases it accepts.
    """
    for key in keys:
        if key in row and row[key] not in (None, ""):
            value = row[key]
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            return str(value).strip()
    return default
