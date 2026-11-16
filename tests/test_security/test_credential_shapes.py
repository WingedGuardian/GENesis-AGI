"""The shared credential-shape table: parity with the hook scrubber, and portability.

Synthetic inputs only; nothing here is a real credential.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.security import credential_shapes as shapes
from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]
TABLE = ROOT / "src" / "genesis" / "security" / "credential_shapes.py"
HOOK = ROOT / "scripts" / "hooks" / "secret_scrub.py"

ALL_SHAPES = shapes.KEY_PREFIX_SHAPES + shapes.STRUCTURED_SHAPES


@pytest.mark.parametrize(
    "ours,hook_name",
    [
        ("KNOWN_KEY_PREFIX_PATTERN", "_KNOWN_KEY_PREFIX_PATTERN"),
        ("STRUCTURED_CREDENTIAL_PATTERN", "_STRUCTURED_CREDENTIAL_PATTERN"),
    ],
)
def test_table_composes_to_the_scrubbers_patterns(ours, hook_name):
    """Until the hook scrubber loads this table, the two copies are held equal
    here: a shape changed in either file alone fails this test."""
    hook = private_module("_shape_parity_secret_scrub", HOOK)
    table_pattern, hook_pattern = getattr(shapes, ours), getattr(hook, hook_name)
    assert table_pattern.pattern == hook_pattern.pattern
    assert table_pattern.flags == hook_pattern.flags


def test_names_are_unique_and_fragments_are_whole_alternatives():
    names = [name for name, _ in ALL_SHAPES]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names)
    for name, fragment in ALL_SHAPES:
        # A fragment is one alternative: grouped alone it must compile to the
        # same thing the composed pattern holds, with no flag leaking out.
        assert re.compile("(?:" + fragment + ")").flags == re.compile("x").flags, name


def test_module_is_stdlib_only():
    tree = ast.parse(TABLE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"__future__", "re"}


def test_module_loads_by_path_without_the_genesis_package():
    """A script that cannot import ``genesis`` loads this file by path."""
    probe = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('_shapes_by_path', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "assert mod.KNOWN_KEY_PREFIX_PATTERN.search('x ' + 'nvapi-' + 'a' * 40)\n"
        "assert not any(m == 'genesis' or m.startswith('genesis.') for m in sys.modules)\n"
        "print('loaded', len(mod.KEY_PREFIX_SHAPES), len(mod.STRUCTURED_SHAPES))\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-I", "-c", probe, str(TABLE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    expected = f"loaded {len(shapes.KEY_PREFIX_SHAPES)} {len(shapes.STRUCTURED_SHAPES)}"
    assert done.stdout.strip() == expected


def test_no_possessive_quantifiers_in_any_pattern():
    """Possessive quantifiers need Python 3.11; the hook's interpreter can be older."""
    possessive = re.compile(r"(?:\*\+|\?\+|\+\+|\{\d+(?:,\d*)?\}\+)")
    compiled = [obj for obj in vars(shapes).values() if isinstance(obj, re.Pattern)]
    assert len(compiled) == 2  # guard: the check below reads real patterns
    assert not [p.pattern for p in compiled if possessive.search(p.pattern)]
    assert possessive.search(r"[A-Z0-9_]++")  # the detector can see one


def test_the_three_run_shape_keeps_bounded_repeats():
    """Linearity is a property of the shapes, so it is pinned on the shape.

    The JWT shape is three runs joined by dots. With unbounded repeats, every
    start that never completes rescans the rest of the input, which is
    quadratic on ordinary non-matching text (measured beside the scrubber's
    copy). Its three repeats must stay bounded.
    """
    jwt = dict(ALL_SHAPES)["jwt"]
    assert not re.search(r"\{\d+,\}|(?<!\\)[*+]", jwt)
    assert len(re.findall(r"\{\d+,\d+\}", jwt)) == 3
