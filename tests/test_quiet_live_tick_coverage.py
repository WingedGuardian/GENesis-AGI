"""Every test module that runs a real awareness tick uses ``quiet_live_tick``.

A real ``AwarenessLoop._on_tick()`` runs the git-health check (writes the
Guardian's verdict and a probe into the repository's shared ``.git``) and the
repo-bundle publish (replaces the Guardian's recovery bundle with this
checkout's HEAD). ``quiet_live_tick`` turns both off; it is opt-in, because
the git-health tests drive those steps directly, so this test keeps a new
tick test from silently missing it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent


def _calls_on_tick(text: str) -> bool:
    """Whether the module CALLS ``._on_tick()``: a comment or docstring that
    only mentions it does not run a tick."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "._on_tick(" in text  # unparseable: judge by text rather than skip it
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_on_tick"
        for node in ast.walk(tree)
    )


def test_a_tick_only_documented_is_not_a_real_tick():
    assert not _calls_on_tick('"""Runs AwarenessLoop._on_tick() for real."""\n# loop._on_tick()\n')
    assert _calls_on_tick("async def t(loop):\n    await loop._on_tick()\n")


def test_every_real_tick_module_is_quiet():
    missing = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "AwarenessLoop" in text and _calls_on_tick(text) and "quiet_live_tick" not in text:
            missing.append(str(path.relative_to(_TESTS)))
    assert not missing, (
        "these modules run a real AwarenessLoop tick without the quiet_live_tick "
        f'fixture (add `pytestmark = pytest.mark.usefixtures("quiet_live_tick")`): {missing}'
    )
