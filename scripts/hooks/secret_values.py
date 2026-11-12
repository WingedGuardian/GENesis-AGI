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
            _pairs = [(n["text"], f"[secrets.env:{n['var']}]") for n in needles]
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


def redact(text: str) -> str:
    """``text`` with every known secret value replaced by its placeholder, longest spelling first."""
    if not text:
        return text
    for needle, placeholder in _load():
        if needle in text:
            text = text.replace(needle, placeholder)
    return text
