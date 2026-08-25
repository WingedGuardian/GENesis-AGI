"""Unit tests for scripts/hooks/destructive_command_guard.py — the rm-guard.

This guard has its OWN tokenizer (it does not use shell_parse.analyze), so it
must independently resolve `rm` through a shell command-position group opener.
It already scans every token, so SPACED control forms (`( rm …`, `then rm …`,
`{ rm …`) are handled; the gap this locks in is the GLUED opener `(rm` — a full
`(rm -rf /)` / `(rm -rf .)` home/root-wipe bypass (2026-08-24 red-team).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import destructive_command_guard as dg  # noqa: E402


def _blocks(cmd: str) -> bool:
    v = dg._rm_violations(cmd)
    return bool(v)  # non-empty list of reasons = block; [] or None = allow


class TestGluedSubshellRm:
    # the bypass: a glued `(rm` opener
    def test_glued_root(self):
        assert _blocks("(rm -rf /)")

    def test_glued_dot(self):
        assert _blocks("(rm -rf .)")

    def test_glued_home(self):
        assert _blocks("(rm -rf ~)")

    def test_glued_in_compound(self):
        assert _blocks("cd /tmp && (rm -rf /)")

    def test_spaced_nested_subshell_blocks(self):
        # `( (rm -rf /) )` is nested subshells — the rm DOES run → block
        assert _blocks("( (rm -rf /) )")


class TestArithmeticNotABypass:
    # `((…))` is bash ARITHMETIC evaluation — it runs NO external command, so a
    # glued `((rm` is NOT a subshell rm and must NOT be blocked (a false-positive
    # over-block corrected in round-2). A real rm hidden in $(…) INSIDE arithmetic
    # is caught by shell_parse's substitution path, not by this guard's tokenizer.
    def test_double_glued_arith_allowed(self):
        assert not _blocks("((rm -rf /))")

    def test_arith_with_trailing_operand_allowed(self):
        assert not _blocks("((rm -rf / 1))")


class TestSpacedControlFormsStillBlock:
    # these already worked (every-token scan) — regression lock
    def test_spaced_subshell(self):
        assert _blocks("( rm -rf / )")

    def test_then(self):
        assert _blocks("then rm -rf /")

    def test_brace(self):
        assert _blocks("{ rm -rf /; }")

    def test_plain(self):
        assert _blocks("rm -rf /")


class TestSafetyNoOverBlock:
    def test_non_recursive_rm_allowed(self):
        assert not _blocks("rm file.txt")

    def test_deep_path_allowed(self):
        # depth >= 4 non-protected path is user-approved-deletable
        assert not _blocks("rm -rf /srv/app/data/build")

    def test_deep_path_glued_allowed(self):
        assert not _blocks("(rm -rf /srv/app/data/build)")

    def test_subshell_without_rm_allowed(self):
        assert not _blocks("(cd /tmp && ls)")

    def test_no_rm_at_all(self):
        assert not _blocks("git status")
