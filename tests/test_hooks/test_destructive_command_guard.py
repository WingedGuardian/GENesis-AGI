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

import pytest

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


class TestLineContinuationFoldsAsTheShellFolds:
    """A backslash-newline is DELETED by the shell, not replaced by whitespace.

    The guard's pre-tokenizer must fold it the same way, or a token the shell
    reads as one is read here as two — and a verdict taken on the two-token
    reading is a verdict on a command the shell never runs. Asserted as a
    PROPERTY over pairs: the guard's answer for the continued form must equal
    its answer for the form the shell actually executes, in both directions.
    """

    @pytest.mark.parametrize(
        ("continued", "joined"),
        [
            ("rm -\\\nrf ~/genesis", "rm -rf ~/genesis"),
            ("rm -r\\\nf /home/u/genesis", "rm -rf /home/u/genesis"),
            ("rm -rf /home/u/gen\\\nesis", "rm -rf /home/u/genesis"),
            ("rm -rf /home/u/a/b/c/d\\\n/e", "rm -rf /home/u/a/b/c/d/e"),
            ("rm -rf /a/b/c/d/e \\\n/f/g", "rm -rf /a/b/c/d/e /f/g"),
        ],
    )
    def test_continued_and_joined_forms_get_the_same_verdict(self, continued, joined):
        assert dg._rm_violations(continued) == dg._rm_violations(joined), (
            f"{continued!r} -> {dg._rm_violations(continued)!r}; "
            f"shell runs {joined!r} -> {dg._rm_violations(joined)!r}"
        )

    def test_positive_control_the_joined_form_is_refused(self):
        """Without this, an implementation that returned None for everything
        would satisfy the equality above."""
        assert _blocks("rm -rf ~/genesis")


class TestEscapedBackslashIsNotAContinuation:
    """An ESCAPED backslash before a newline does NOT continue the line.

    The class above folds `\\<newline>` because the shell deletes it. That is
    true only when the backslash is ITSELF unescaped — i.e. an ODD-length run.
    In an EVEN-length run every backslash is already escaped by its neighbour,
    so the final one is a literal character and the newline that follows is a
    real command separator: the shell runs TWO commands.

    Folding it anyway deletes that separator, glues the next command's first
    word onto the previous token (`printf x` + `rm` -> `xrm`), and the guard
    then never sees an `rm` token at all. It returns "no violations" — not
    "unparseable" — so `main()`'s legacy-regex fallback, which only runs when
    tokenizing FAILED, never fires either. The command is allowed and the shell
    runs it.

    Same property as the class above, asserted on the other side of the parity:
    the verdict for the escaped form must equal the verdict for the form the
    shell actually executes.
    """

    @pytest.mark.parametrize("run_len", [2, 4])
    def test_even_run_preserves_the_separator(self, run_len):
        backslashes = "\\" * run_len
        escaped = f"printf x{backslashes}\nrm -rf /"
        # What the shell really runs: two commands. `;` is the separator form.
        as_shell_runs = f"printf x{backslashes} ; rm -rf /"

        # Guard-the-guard: this fixture must actually carry an EVEN-length
        # backslash run immediately before the newline, or it tests nothing.
        head = escaped.split("\n", 1)[0]
        trailing = len(head) - len(head.rstrip("\\"))
        assert trailing == run_len and run_len % 2 == 0, (
            f"fixture lost its property: trailing run {trailing}, expected even {run_len}"
        )

        assert dg._rm_violations(escaped) == dg._rm_violations(as_shell_runs), (
            f"{escaped!r} -> {dg._rm_violations(escaped)!r}; "
            f"shell runs {as_shell_runs!r} -> {dg._rm_violations(as_shell_runs)!r}"
        )
        assert _blocks(escaped), "an escaped backslash hid a destructive command"

    def test_odd_run_still_folds(self):
        """Cheap equivalence lock on the odd run — NOT an over-correction constraint.

        Measured against a never-fold implementation, BOTH sides return ``[]``,
        so this passes and constrains nothing in that direction. The
        over-correction guarantee is carried entirely by
        ``TestLineContinuationFoldsAsTheShellFolds`` above, which does fail
        (4 cases) under never-fold. Do not read this as proof the fix cannot
        over-correct; the docstring said exactly that once and was wrong.
        """
        assert dg._rm_violations("rm -rf /a/b/c/d\\\n/e") == dg._rm_violations("rm -rf /a/b/c/d/e")

    def test_boundary_one_backslash_folds_two_does_not(self):
        """The odd/even boundary is the whole fix, asserted directly.

        The odd-run target is ``/`` — depth 1 — deliberately. An earlier
        revision used a depth-5 target, which is ALLOWED whether or not the
        fold happens, so the assertion passed under a never-fold implementation
        too: vacuous in precisely the direction it claims to test. At ``/`` the
        two implementations disagree — folding joins ``x``+``rm`` so no rm token
        exists (allow), never-folding leaves a real ``rm -rf /`` (block).
        """
        assert not _blocks("printf x\\\nrm -rf /"), (
            "odd run is a continuation: the words join, no second command exists"
        )
        assert _blocks("printf x\\\\\nrm -rf /"), (
            "even run is a literal backslash + a real separator: rm must be seen"
        )
