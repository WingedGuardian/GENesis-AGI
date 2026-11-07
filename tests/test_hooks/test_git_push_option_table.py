"""Drift test for the measured git-push option grammar in ``shell_parse``."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
import shell_parse as sp  # noqa: E402

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git CLI not installed")

_OPTION_TOKEN = re.compile(r"(?<![\w-])--[\w-]+(?:\[[^\]]+\])?(?:=[^\s,]+)?|(?<![\w-])-[A-Za-z0-9]")
_VALUE_PLACEHOLDER = re.compile(r"(?:<[^>]+>|\([^)]*\))")
_LISTED_ONLY_IN_NEWER_GIT = frozenset({"branches", "verify"})


def _measured_push_options() -> tuple[dict[str, str], frozenset[str]]:
    result = subprocess.run([GIT, "push", "-h"], capture_output=True, text=True, timeout=15)
    assert result.returncode in (0, 129), result.stderr

    options: dict[str, str] = {}
    short_value_letters: set[str] = set()
    for line in (result.stdout + result.stderr).splitlines():
        spec = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
        spec = re.sub(r"--\[no-\]", "--", spec)
        if not spec.startswith("-"):
            continue
        tokens = _OPTION_TOKEN.findall(spec)
        if not tokens:
            continue
        optional = any("[=" in token for token in tokens)
        required = any("=" in token and "[=" not in token for token in tokens)
        required |= _VALUE_PLACEHOLDER.search(spec) is not None
        arity = "optional" if optional else ("value" if required else "none")
        for token in tokens:
            if token.startswith("--"):
                name = token[2:].split("[", 1)[0].split("=", 1)[0]
                options[name] = arity
            elif arity in {"value", "optional"}:
                short_value_letters.add(token[1])
    return options, frozenset(short_value_letters)


def test_push_option_table_matches_git_help() -> None:
    options, short_value_letters = _measured_push_options()
    assert set(options) <= set(sp.GIT_PUSH_LONG_OPTIONS)
    assert set(sp.GIT_PUSH_LONG_OPTIONS) - set(options) <= _LISTED_ONLY_IN_NEWER_GIT
    assert options == {name: sp.GIT_PUSH_LONG_OPTIONS[name] for name in options}
    assert short_value_letters == sp.GIT_PUSH_SHORT_VALUE_LETTERS
