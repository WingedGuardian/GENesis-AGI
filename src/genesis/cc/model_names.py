"""Display names for model ids, e.g. ``claude-opus-5-5`` -> ``Opus 5.5`` (#4850).

A rule, not a table: a table goes stale at every model release, and a stale entry
is how a header names the wrong model. Ids the rule does not recognise (another
vendor's backend, a fallback route) pass through without a context-window suffix.
"""

from __future__ import annotations

import re

from genesis.cc.types import VALID_MODEL_NAMES, model_name_supports_effort

_CLAUDE_ID = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-\d{8})?$"
)


def display_name(model_id: str) -> str:
    base = (model_id or "").strip().split("[", 1)[0]
    match = _CLAUDE_ID.match(base.lower())
    if match:
        version = match["major"] + (f".{match['minor']}" if match["minor"] else "")
        return f"{match['family'].title()} {version}"
    if base.lower() in VALID_MODEL_NAMES:
        return base.title()
    return base


def effort_label(effort: str) -> str:
    """Display form of an effort level for a header: ``xhigh`` -> ``xHigh``."""
    value = (effort or "").strip().lower()
    return "xHigh" if value == "xhigh" else value.title()


def header_text(model_id: str, effort: str) -> str:
    """The relay's reply header: ``[Opus 5.5 / High]``, or ``[Haiku 4.5]`` for a
    model that takes no effort setting (the invoker never passes it one), so the
    header names only what the run used. Empty when there is no model name."""
    label = display_name(model_id)
    if not label:
        return ""
    shown = effort_label(effort) if model_name_supports_effort(model_id) else ""
    return f"[{label} / {shown}]" if shown else f"[{label}]"
