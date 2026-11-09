"""Visible escapes for human terminal previews; stored values remain intact."""

from __future__ import annotations

import json
import re

from genesis.security.sanitizer import _CONTROL_RUN_RE

_SURROGATES = re.compile(r"[\ud800-\udfff]+")


def terminal_text(value) -> str:
    """Escape control/invisible characters without losing ordinary Unicode."""
    text = "NULL" if value is None else str(value)

    def escaped(match):
        return json.dumps(match.group(), ensure_ascii=True)[1:-1]

    return _SURROGATES.sub(escaped, _CONTROL_RUN_RE.sub(escaped, text))
