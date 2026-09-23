"""Drift test for the gh flag grammar in ``shell_parse`` (#2209).

``_GH_FLAG_TABLE`` is MEASURED from ``gh help <group> <sub>`` output. A gh
release that moves a flag's arity (a bool gaining a placeholder, a value flag
becoming a bundle, a new flag appearing) silently changes what
``gh_command``/``gh_pr_subcommand`` consume — and every downstream gate
inherits the error. This test re-reads the help text and fails when the model
no longer matches the installed CLI, so the table is checked rather than
trusted.

Skips when ``gh`` is not on PATH: the table was generated on 2.78.0 and the
test exists to catch a DIFFERENT version disagreeing with it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS / "hooks"))
import shell_parse as sp  # noqa: E402

GH = shutil.which("gh")
pytestmark = pytest.mark.skipif(GH is None, reason="gh CLI not installed")

#: One flags-section line: ``  -a, --assignee login       <desc>``. Flag names
#: are ``-x``/``--long`` tokens separated by ``,``; a following bare token is
#: the placeholder that makes the flag take a value.
_FLAG_LINE = re.compile(r"^\s+((?:-\w|--[\w-]+)(?:\s*,\s*(?:-\w|--[\w-]+))*)(?:\s+(\S+))?\s{2,}\S")
_NAME = re.compile(r"^(?:-\w|--[\w-]+)$")
_SECTION = re.compile(r"^\s{0,3}[A-Z][A-Z ]*$")


def _measured_flags(group: str, sub: str) -> tuple[frozenset[str], frozenset[str]]:
    """Re-derive (value_flags, bool_flags) for one table row from live help."""
    cmd = [GH, "help", group] + ([sub] if sub else [])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, f"`{' '.join(cmd)}` exited {out.returncode}: {out.stderr}"
    value: set[str] = set()
    boolean: set[str] = set()
    in_flags = False
    for line in out.stdout.splitlines():
        if _SECTION.match(line):
            in_flags = "FLAGS" in line
            continue
        if not in_flags or not line.strip():
            continue
        m = _FLAG_LINE.match(line)
        if m is None:
            continue
        names = [t for t in re.split(r"\s*,\s*", m.group(1)) if _NAME.match(t)]
        (value if m.group(2) else boolean).update(names)
    return frozenset(value), frozenset(boolean)


def test_flag_table_matches_gh_help():
    """Every modeled row equals what the installed gh's own help declares."""
    for (group, sub), expected in sp._GH_FLAG_TABLE.items():
        value, boolean = _measured_flags(group, sub)
        assert value == expected[0], (
            f"gh help {group} {sub}: value-flag set drifted\n"
            f"  table only: {sorted(expected[0] - value)}\n"
            f"  help  only: {sorted(value - expected[0])}"
        )
        assert boolean == expected[1], (
            f"gh help {group} {sub}: valueless-flag set drifted\n"
            f"  table only: {sorted(expected[1] - boolean)}\n"
            f"  help  only: {sorted(boolean - expected[1])}"
        )


def test_unions_are_derived():
    """The union sets must be exactly the union of the rows — no hand edits."""
    assert frozenset().union(
        *(v for v, _ in sp._GH_FLAG_TABLE.values())
    ) == sp._GH_ALL_VALUE_FLAGS
    assert frozenset().union(
        *(b for _, b in sp._GH_FLAG_TABLE.values())
    ) == sp._GH_ALL_BOOL_FLAGS


class TestGhCommand:
    """The shared resolver: union flags before the path, row table after."""

    def test_leaf_command_union_value_flag_before_group(self):
        # `gh -X PATCH api ...`: -X is a value flag under `api` — it eats
        # PATCH BEFORE the group resolves (measured behaviour, issue #2209).
        inv = sp.gh_command(["gh", "-X", "PATCH", "api", "repos/o/r"])
        assert inv is not None
        assert inv.group == "api" and inv.subcommand is None
        assert inv.positionals == ("repos/o/r",)

    def test_union_flag_eats_group_word(self):
        # `gh -f pr create --help` FAILS in real gh ("unknown command
        # create") because -f (an api value flag) ate `pr`. The resolver
        # must reproduce that: group becomes "create", reported as
        # unmodelled rather than silently correct.
        inv = sp.gh_command(["gh", "-f", "pr", "create"])
        assert inv is not None
        assert inv.group == "create"

    def test_subcommand_value_flag_between_group_and_verb(self):
        # `gh pr -c note close 1`: -c takes a value under `pr close`, so
        # `note` is consumed and the verb is still close.
        inv = sp.gh_command(["gh", "pr", "-c", "note", "close", "1"])
        assert inv is not None
        assert inv.group == "pr" and inv.subcommand == "close"
        assert inv.positionals == ("1",)

    def test_same_flag_different_arity_by_row(self):
        # The measured collision the per-subcommand table exists for:
        # `-f` is a VALUE under `api` but a BOOL under `pr create`.
        inv = sp.gh_command(["gh", "pr", "create", "-f", "extra"])
        assert inv.group == "pr" and inv.subcommand == "create"
        assert inv.positionals == ("extra",)

    def test_unmodelled_flag_is_reported(self):
        inv = sp.gh_command(["gh", "pr", "view", "1", "--nonexistent"])
        assert inv.subcommand == "view"
        assert "--nonexistent" in inv.unmodelled

    def test_double_dash_ends_options(self):
        inv = sp.gh_command(["gh", "api", "--", "--not-a-flag"])
        assert inv.positionals == ("--not-a-flag",)
        assert not inv.unmodelled

    def test_glued_and_equals_value_spellings(self):
        assert sp.gh_command(["gh", "pr", "-Ro/r", "view", "1"]).subcommand == "view"
        inv = sp.gh_command(["gh", "--repo=o/r", "pr", "view", "1"])
        assert inv.group == "pr" and inv.subcommand == "view"

    def test_not_gh(self):
        assert sp.gh_command(["git", "push"]) is None
        assert sp.gh_command(["gh"]) is None


class TestGhPrSubcommand:
    """The fail-closed anywhere-scan keeps its semantics on the union table."""

    def test_value_flag_between_pr_and_verb(self):
        assert sp.gh_pr_subcommand(["gh", "pr", "-R", "o/r", "merge", "5"]) == "merge"
        assert sp.gh_pr_subcommand(["gh", "pr", "-c", "note", "close", "1"]) == "close"

    def test_unknown_dash_is_not_consumed(self):
        # Fail-closed: an unmodelled flag is skipped WITHOUT eating the next
        # word, so the verb is still found rather than hidden behind it.
        assert sp.gh_pr_subcommand(["gh", "pr", "--bogus", "merge", "5"]) == "merge"
