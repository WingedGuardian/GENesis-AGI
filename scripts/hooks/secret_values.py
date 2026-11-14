"""Replace this install's known secrets.env values in text a hook is about to emit or store.

The values, and every spelling of them, come from the genesis-secret-redact plugin's own loader
(``plugins/genesis-secret-redact/bin/known_values.py``), loaded by path, so a hook and the plugin agree on what is
a secret. A value becomes ``[secrets.env:VAR]``, the plugin's placeholder.

Used where Claude Code keeps a hook's text as its own record, out of the plugin's reach: proactive recall
(UserPromptSubmit output is stored as a ``hook_success`` record, measured on 2.1.299) and the session observer
(PostToolUse hooks receive a tool's output before the plugin redacts it, measured).

Loaded once per process (a hook is one short process). If the values cannot be loaded, text passes through
unchanged: a hook must never break a prompt or a tool call over this.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_LOADER = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "genesis-secret-redact"
    / "bin"
    / "known_values.py"
)
_pairs: list[tuple[str, str]] | None = None


def _load() -> list[tuple[str, str]]:
    global _pairs
    if _pairs is None:
        try:
            spec = importlib.util.spec_from_file_location("_hook_known_values", _LOADER)
            if spec is None or spec.loader is None:
                raise ImportError(str(_LOADER))
            known = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(known)
            needles = known.load(known.repo_root(str(_LOADER.parents[3])))["needles"]
            _pairs = [(n["text"], n["placeholder"]) for n in needles]
        except Exception as exc:  # noqa: BLE001 - see the module docstring: never break the hook
            import sys

            print(
                f"secret_values: known values not loaded ({type(exc).__name__}); text is not filtered",
                file=sys.stderr,
            )
            _pairs = []
    return _pairs


def redact_deep(value):
    """``value`` (a JSON-shaped dict/list/str) with ``redact`` applied to every string in it."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    return value


#: The shortest start of a value treated as that value when the text was clipped through it.
PARTIAL_MIN = 10
#: A value whose start can be matched alone: token-like, so its first characters are not ordinary words or a URL
#: scheme that other text shares.
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_+/=.-]+")


def redact(text: str, *, partial: bool = False) -> str:
    """``text`` with every known secret value replaced by its placeholder, longest spelling first.

    ``partial`` also replaces the start of a token-like value (``PARTIAL_MIN`` characters or more) where text was
    clipped through it before it reached this hook (proactive recall: the server clips each memory to 200 or 300
    characters first). Only for short text: it costs one scan per value.
    """
    if not text:
        return text
    pairs = _load()
    for needle, placeholder in pairs:
        if needle in text:
            text = text.replace(needle, placeholder)
    if partial:
        for needle, placeholder in pairs:
            if (
                len(needle) <= PARTIAL_MIN
                or not _TOKEN_LIKE.fullmatch(needle)
                or needle[:PARTIAL_MIN] not in text
            ):
                continue
            for k in range(len(needle) - 1, PARTIAL_MIN - 1, -1):
                if needle[:k] in text:
                    text = text.replace(needle[:k], placeholder)
    return text
