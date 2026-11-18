"""Display names for model ids, e.g. ``claude-opus-5-5`` -> ``Opus 5.5`` (#4850).

A rule, not a table: a table goes stale at every model release, and a stale entry
is how a header names the wrong model. Ids the rule does not recognise (another
vendor's backend, a fallback route) pass through without a context-window suffix.
"""

from __future__ import annotations

import re

_CLAUDE_ID = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-\d{8})?$"
)
_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable"})


def display_name(model_id: str) -> str:
    base = (model_id or "").strip().split("[", 1)[0]
    match = _CLAUDE_ID.match(base.lower())
    if match:
        version = match["major"] + (f".{match['minor']}" if match["minor"] else "")
        return f"{match['family'].title()} {version}"
    if base.lower() in _ALIASES:
        return base.title()
    return base
