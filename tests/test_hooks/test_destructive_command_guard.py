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


class TestContinuationInsideAComment:
    """A comment ends at the newline — the shell does not continue it.

    The fold was a whole-string regex with no comment model, so a backslash at
    the end of a `#` comment was treated as a continuation and the following
    command was glued into the comment text. No `rm` token then existed, and
    because tokenizing SUCCEEDED the legacy-regex fallback (which only fires
    when tokenizing FAILS) never ran either.

    This guard is the only one that covers shallow non-protected targets, so
    there is no compensating control for these shapes.

    Both error directions are unsafe here, which is why the fix tracks real
    state rather than approximating: failing to fold a genuine continuation
    splits a word the shell joins and can hide the recursive-force flags (the
    bypass recorded in ``_rm_violations``), while folding one the shell does not
    join hides the command itself.
    """

    # Every context a `#` can follow, with the flag recording whether BASH opens a
    # comment there — derived by running `<prefix>#note \`⏎`touch <marker>` under
    # `bash --norc --noprofile` and checking for the marker FILE.
    #
    # An earlier revision of this list held four contexts that all happened to be
    # ones the code already got right (three spaces and a subshell `)`), so the
    # suite was green while two bypass classes were live. A table that only
    # contains passing cases tests nothing; these are the cases that DISAGREED.
    _COMMENT_CONTEXTS = [
        ("space", "echo hi  # note a", True),
        ("tight", "ls #x", True),
        ("trailing-word", "true # trailing", True),
        ("subshell-close", "(true)#n", True),
        ("semicolon", "echo hi;# note a", True),
        ("pipe", "echo hi|# note a", True),
        ("and-and", "true&&# note a", True),
        ("or-or", "false||# note a", True),
        ("background-amp", "true&# note a", True),
    ]

    @pytest.mark.parametrize(
        ("label", "prefix", "bash_opens_comment"),
        _COMMENT_CONTEXTS,
        ids=[c[0] for c in _COMMENT_CONTEXTS],
    )
    def test_a_comment_ending_in_a_backslash_does_not_hide_the_next_rm(
        self, label, prefix, bash_opens_comment
    ):
        assert bash_opens_comment, "table lists only real comment contexts"
        assert _blocks(f"{prefix}\\\nrm -rf /"), (
            f"{label}: the shell ends the comment at the newline and runs the rm"
        )

    # The opposite direction, and it is a bypass too: these look like comment
    # starts to a naive rule but are MID-WORD to the shell, so the continuation
    # after them is real. Refusing to fold splits `-r` from `f` and the guard
    # stops seeing a recursive-force removal at all.
    @pytest.mark.parametrize(
        ("label", "prefix"),
        [
            ("command-substitution", "echo $(date)#x"),
            ("arithmetic-expansion", "echo $((1))#x"),
            ("nested-substitution", "echo $(echo $(date))#x"),
            ("escaped-space", "echo a\\ #x"),
        ],
        ids=[
            "command-substitution",
            "arithmetic-expansion",
            "nested-substitution",
            "escaped-space",
        ],
    )
    def test_a_mid_word_hash_does_not_suppress_a_real_fold(self, label, prefix):
        assert _blocks(f"{prefix}; rm -r\\\nf /"), (
            f"{label}: the shell joins these, so the guard must still see -rf"
        )

    def test_the_shallow_target_class_this_guard_uniquely_covers(self):
        """`/usr` is caught by nothing else in the chain — the control matters."""
        assert _blocks("rm -rf /usr"), "control: one line must block"
        assert _blocks("echo hi  # note a\\\nrm -rf /usr")

    def test_a_hash_inside_quotes_is_not_a_comment_and_must_still_fold(self):
        """The dangerous inverse: declining to fold hides the flags.

        A `#` inside a quoted string opens no comment, so a continuation after
        it is real. Treating it as a comment would split `-r`/`f` across the
        newline and the guard would no longer see a recursive-force removal.
        """
        assert _blocks("echo '# not a comment' && rm -r\\\nf /")

    def test_a_comment_on_an_earlier_line_does_not_disarm_a_later_fold(self):
        """Comment state must clear at the newline, not persist down the command."""
        assert _blocks("echo hi # done\nrm -r\\\nf /")


class TestWordFormParentheses:
    """Not every `)` ends a word — and reading that wrong is a bypass both ways.

    The first fix recognized only `$(` as a parenthesis whose `)` stays inside a
    word. Every other WORD-FORM parenthesis — process substitution, extglob,
    array assignment — closed into what the code called a word boundary, so a
    glued `#` faked a comment, the continuation after it was not folded, and the
    command on the next line vanished from the token stream. Both directions
    were measured through the live hook, with ``main`` as the control column so
    a pre-existing gap could not be reported as a regression; the shapes are the
    parametrised rows below, where they are fixture data rather than prose.
    Spelling one out here would publish a recipe, and this repository is public.

    The opposite error is a bypass too, so the members cannot be guessed in
    either direction. Each row below carries the answer BASH gives, measured
    with ``<prefix>#note; touch <marker>`` under bash 5.2 — the marker appears
    iff the ``; touch`` escaped the comment, i.e. iff ``#`` opened none.

    That spelling replaced an earlier ``<prefix>#note \\``⏎``touch <marker>``
    probe, which is CONFOUNDED: folding leaves ``<prefix>#note touch <marker>``,
    and when the prefix is an assignment (``a=(x)``) the first word is an
    assignment prefix, so ``touch`` runs as the command word and the marker
    appears whether or not a comment opened. That confound reported array
    assignment as a word boundary, which bash says it is not.
    """

    # `)` closes a WORD-FORM parenthesis: the word continues, a glued `#` is
    # ordinary text, and the continuation on the next line is REAL. Refusing to
    # fold it splits `-r` from `f` and no recursive-force rm is seen.
    _WORD_FORM = [
        ("process-substitution-in", "echo <(true)#x"),
        ("process-substitution-out", "echo >(true)#x"),
        ("process-substitution-nested", "echo <(echo <(true))#x"),
        ("process-substitution-inner-subshell", "echo <( (true) )#x"),
        # The extglob openers are a DIFFERENT evidentiary case from the rows
        # around them, and saying so is the point. Under the non-interactive
        # `bash -c` this hook actually sees, extglob is OFF and these are a
        # SYNTAX ERROR — so the marker probe records "no marker" and a reader
        # scores that as "a comment opened", which is a fabricated answer rather
        # than a measurement. Their bash answer above was taken under
        # `-O extglob`; they are kept because a caller may have it set, and the
        # set must be right for that case too, but they are NOT evidence about
        # the default configuration.
        #
        # `!(` used to sit in this list and has been REMOVED from the set: with
        # extglob off it is `!` negation plus a subshell, whose `)` really does
        # let a `#` open a comment. Measured both ways; the two answers are
        # incompatible and the runtime default wins. It now lives in _COMMAND_FORM.
        # `+(` must be probed in a NON-INITIAL position: at the start of a `-c`
        # string bash reads it as an option (`+(: invalid option`) and the probe
        # reports nothing, which is easy to mistake for a real answer. Measured
        # here as word-form under `-O extglob`.
        ("extglob-at", "echo @(zzz)#x"),
        ("extglob-star", "echo *(zzz)#x"),
        ("extglob-plus", "echo +(zzz)#x"),
        ("extglob-question", "echo ?(zzz)#x"),
        ("array-assignment", "arr=(a b)#x"),
        ("array-append", "arr+=(a b)#x"),
        ("array-declare", "declare -a arr=(a b)#x"),
        ("command-substitution", "echo $(true)#x"),
        ("arithmetic-expansion", "echo $((1+1))#x"),
        ("command-substitution-inner-subshell", "echo $( (true) )#x"),
    ]

    @pytest.mark.parametrize(("label", "prefix"), _WORD_FORM, ids=[c[0] for c in _WORD_FORM])
    def test_a_word_form_close_paren_does_not_fake_a_comment(self, label, prefix):
        assert _blocks(f"{prefix}; rm -r\\\nf /usr"), (
            f"{label}: bash keeps the word open, so the continuation is real "
            "and the guard must still see -rf"
        )

    # `)` closes a COMMAND-FORM parenthesis: it ends a command, so a glued `#`
    # DOES open a comment and the trailing backslash is comment text, not a
    # continuation. Folding it anyway glues the next command onto the comment
    # and the `rm` token disappears.
    _COMMAND_FORM = [
        ("subshell", "(true)#note"),
        ("subshell-nested", "( (true) )#note"),
        ("arithmetic-command", "((1+1))#note"),
        ("arithmetic-command-nested", "((1+(2)))#note"),
        # Regression lock for a depth counter that never unwound: `$(` was
        # counted twice (once by the `$` lookahead, once by the `(` itself), so
        # after ANY command substitution every later `)` was read as mid-word
        # and this shape was allowed while bash ran the rm.
        ("subshell-after-substitution", "echo $(true); (true)#note"),
        ("arith-command-after-substitution", "echo $(true); ((1+1))#note"),
        ("subshell-after-process-substitution", "echo <(true); (true)#note"),
        # `!(` with extglob OFF — the default for the `bash -c` this hook sees —
        # is `!` negation applied to a SUBSHELL, so its `)` ends a command and a
        # glued `#` opens a real comment. It reads as an extglob pattern only
        # with `shopt -s extglob`; the two answers are incompatible and the
        # runtime default decides, which is why `!` is not in the prefix set.
        ("bang-subshell-extglob-off", "!(true)#note"),
    ]

    @pytest.mark.parametrize(("label", "prefix"), _COMMAND_FORM, ids=[c[0] for c in _COMMAND_FORM])
    def test_a_command_form_close_paren_still_opens_a_comment(self, label, prefix):
        assert _blocks(f"{prefix}\\\nrm -rf /usr"), (
            f"{label}: bash ends the comment at the newline and runs the rm"
        )

    # A command-form paren NESTED INSIDE a word-form one. This needs its own
    # payload — the construct has to be closed — which is exactly why it was
    # missed: the sibling table's `*-inner-subshell` rows put the `#` after the
    # OUTER `)`, a position the code already handled, so they passed while these
    # were open. A classification that INHERITS word-form-ness down the nesting
    # cannot see them at all; each paren has to be judged from its own opener.
    @pytest.mark.parametrize(
        ("label", "payload"),
        [
            ("inside-command-substitution", "echo $( (true)#c\\\nrm -rf /usr\n)"),
            ("inside-process-substitution", "cat <( (true)#c\\\nrm -rf /usr\n)"),
        ],
        ids=["inside-command-substitution", "inside-process-substitution"],
    )
    def test_a_subshell_inside_a_word_form_paren_still_opens_a_comment(self, label, payload):
        # MEASURED: bash runs the rm in both — the inner `)` ends a command, so
        # `#c` opens a real comment and the backslash before the newline is
        # comment text rather than a continuation.
        assert _blocks(payload), f"{label}: bash runs the rm; the guard must see it"

    def test_a_command_substitution_depth_returns_to_zero(self):
        """The counter must unwind, not leak — the mechanism behind the above.

        Asserted on the fold's own output rather than a verdict, so a future
        change that re-breaks the arithmetic is caught at the source: with a
        leaked depth the trailing `)` reads as mid-word and the fold happens.
        """
        folded = dg._fold_continuations("echo $(true); (true)#note \\\nZZZ")
        assert "\nZZZ" in folded, "comment must survive as its own line"
