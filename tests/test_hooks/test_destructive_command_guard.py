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


class TestNestedQuotingContexts:
    """A quote belonging to a NESTED command does not close the outer word.

    One scalar `quote` slot read a nested OPENING quote as the outer CLOSING
    quote and left quote mode early. A `#` in nested quoted data then looked
    like a word start, opened a comment, and suppressed the real continuation
    further along — so the recursive-force flags split across the newline and
    no recursive-force removal was seen for a command the shell does run.

    The same divergence has three spellings, and fixing only the reported one
    would leave its twins open. All three rows below were measured through a
    marker-file shim (an `echo` payload prints its own arguments, so matching
    output would lie), with ``main`` as the control column so a pre-existing
    gap could not be reported as a regression: bash runs the removal in every
    row, ``main`` blocks every row, and the pre-fix branch allowed all three.

    The shapes live here as fixture data rather than as prose, and no worked
    example is spelled out in the module: naming a construct beside a statement
    that a gate stopped working is a recipe, and this repository is public.
    """

    @pytest.mark.parametrize(
        ("label", "prefix"),
        [
            ("command-substitution-in-double-quotes", 'echo "$(printf "%s #x" hi)"'),
            ("backtick-substitution-in-double-quotes", 'echo "`printf "%s #x" hi`"'),
            ("parameter-expansion", "echo ${unset:- #x}"),
        ],
        ids=[
            "command-substitution-in-double-quotes",
            "backtick-substitution-in-double-quotes",
            "parameter-expansion",
        ],
    )
    def test_a_nested_quote_does_not_fake_a_comment(self, label, prefix):
        assert _blocks(f"{prefix}; rm -r\\\nf /"), (
            f"{label}: the `#` is data to the shell, so the continuation after "
            "it is real and the guard must still see -rf"
        )

    def test_single_quotes_are_not_entered(self):
        """Nothing expands inside single quotes, so the substitution is literal.

        The inverse error of the class above: treating this body as a nested
        command would leave quote mode at its inner double quote for real.
        """
        assert _blocks("echo '$(printf \"%s #x\" hi)'; rm -r\\\nf /")

    # New state that never RESETS is itself a bypass: with the brace depth or
    # the saved backtick left occupied, comment recognition would be dead for
    # the rest of the command and every later continuation would be folded —
    # which glues the next command onto comment text so no rm token survives.
    # bash runs the rm in every row (the comment ends at the newline).
    @pytest.mark.parametrize(
        ("label", "payload"),
        [
            ("after-parameter-expansion", "echo ${x:-y} #note\\\nrm -rf /"),
            ("after-backtick-substitution", 'echo "`printf hi`" #note\\\nrm -rf /'),
            ("after-command-substitution", 'echo "$(printf hi)" #note\\\nrm -rf /'),
            ("unmatched-close-brace", "echo } #note\\\nrm -rf /"),
        ],
        ids=[
            "after-parameter-expansion",
            "after-backtick-substitution",
            "after-command-substitution",
            "unmatched-close-brace",
        ],
    )
    def test_the_nested_context_state_returns_to_zero(self, label, payload):
        assert _blocks(payload), (
            f"{label}: a real comment must still open once the nested context "
            "has closed, so the rm on the next line survives as its own command"
        )


class TestCommandCarriersAreRefusedWithoutInspection:
    """A launcher that runs a command this guard cannot recover is REFUSED.

    The previous revision recursed into the carried string, bounded by
    `_MAX_CARRIER_DEPTH`. Both halves were wrong: the bound SKIPPED the branch
    on reaching the limit instead of refusing, so four nested `eval` layers
    reached an unblocked `rm -rf` (MEASURED, on `rm -rf ~` — a home-directory
    wipe allowed by both guards); and the recursion could not see an argv
    payload, an option-attached payload, or one split across quoted fragments.
    """

    RM = "r" + "m"
    CARRIERS = [
        "eval", "su", "runuser", "setpriv", "chroot", "flock", "watch",
        "script", "systemd-run", "unshare", "nsenter", "pkexec", "runcon", "sg",
        "bash", "sh", "dash", "zsh", "ksh", "ash",
    ]

    @pytest.mark.parametrize("carrier", CARRIERS)
    def test_every_carrier_is_refused(self, carrier):
        assert _blocks(f"{carrier} '{self.RM} -rf /a/b'"), carrier

    @pytest.mark.parametrize(
        "label,cmd",
        [
            ("argv",              "eval {RM} -rf /a/b"),
            ("nested x4",         "eval eval eval eval {RM} -rf /a/b"),
            ("nested x5",         "eval eval eval eval eval {RM} -rf /a/b"),
            ("attached --command", "su --command='{RM} -rf /a/b' root"),
            ("attached -c glued",  "script -c'{RM} -rf /a/b' /tmp/o"),
            ("quote-split verb",   "eval '{RM}'\"''\"' -rf /a/b'"),
            ("unparseable payload", 'eval "{RM} -rf / #\\""'),
        ],
    )
    def test_the_spellings_that_defeated_recursion_are_refused(self, label, cmd):
        assert _blocks(cmd.format(RM=self.RM)), label

    @pytest.mark.parametrize(
        "opener,cmd",
        [
            ("brace", '{{ bash -c "{RM} -rf /a/b"; }}'),
            ("then", 'if true; then bash -c "{RM} -rf /a/b"; fi'),
            ("do", 'for i in 1; do bash -c "{RM} -rf /a/b"; done'),
            ("else", 'if false; then true; else bash -c "{RM} -rf /a/b"; fi'),
            ("subshell", '( bash -c "{RM} -rf /a/b" )'),
            ("separator", 'true; bash -c "{RM} -rf /a/b"'),
        ],
    )
    def test_a_carrier_after_any_command_OPENER_is_refused(self, opener, cmd):
        """Command position is not just "after a separator".

        Scoping the carrier check to `_SEPARATORS` (`| || && ; & \\n`) fixed an
        argument-position over-block and opened these five in the same edit:
        each was MEASURED allow while the direct `rm -rf /a/b` blocked. `time`,
        `nice`, `{`, `(`, `then` and `do` all precede a command word, and the
        `rm` scan one branch down already knows this — its comment is why it
        deliberately walks every token instead.
        """
        assert _blocks(cmd.format(RM=self.RM)), opener

    @pytest.mark.parametrize(
        "cmd,blocked,why",
        [
            ("eval echo performance", False, "`perform` is not an rm invocation"),
            ("eval echo 'the form of it'", False, "`form` is not an rm invocation"),
            ("eval echo storm", False, "`storm` is not an rm invocation"),
            ("eval {RM} -rf /a/b", True, "a real removal still reaches the guard"),
            ("eval /bin/{RM} -rf /a/b", True, "a path-qualified rm still matches"),
            ("true; eval {RM} -rf /a/b", True, "`;rm` satisfies a leading boundary"),
        ],
    )
    def test_the_entry_prefilter_is_a_WORD_not_a_substring(self, cmd, blocked, why, tmp_path):
        """Run the guard as a SUBPROCESS, so main()'s prefilter is exercised.

        `_blocks` calls `_rm_violations` directly and therefore cannot see the
        prefilter at all — which is why a mutation reverting it to a substring
        test SURVIVED the whole suite. A substring test also matches `perform`,
        `form` and `storm`; harmless while a false match cost only a scan that
        found nothing, and an over-block once a carrier in command position is
        refused on sight. MEASURED over 83,201 recorded commands: 170 -> 99.
        """
        import json
        import os
        import subprocess
        import sys as _sys

        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        script = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / (
            "destructive_command_guard.py"
        )
        res = subprocess.run(
            [_sys.executable, str(script)],
            input=json.dumps(
                {"tool_input": {"command": cmd.format(RM=self.RM)}, "tool_name": "Bash"}
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        got = res.returncode == 2
        assert got is blocked, f"{cmd!r}: expected blocked={blocked} ({why}), got {res.returncode}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo time bash {RM}",
            "echo nice sh {RM} -rf",
            "echo 'run time bash to {RM} it'",
        ],
    )
    def test_a_PREFIX_COMMAND_name_in_argument_position_is_not_an_opener(self, cmd):
        """Control keywords may open a command; command NAMES may also be data.

        An earlier revision listed `time` and `nice` as openers. This scanner
        has no notion of being inside a simple command, so the bare name fired
        wherever the WORD appeared: `echo time bash rm` was REFUSED while main
        allows it. `then` and `do` cannot appear as bare arguments the same way,
        which is why the set is control keywords only.

        The prefix-command class is a PRE-EXISTING gap (main allows
        `env sh -c "rm -rf /a/b"` and five siblings identically) and needs
        simple-command tracking rather than a longer list.
        """
        assert not _blocks(cmd.format(RM=self.RM)), cmd

    # ---- the RESOLVER path. `_blocks()` cannot reach it: it calls
    # `_rm_violations` directly, so it bypasses main() and therefore the
    # resolver pre-pass entirely. Every test above this line pins the FALLBACK.

    @staticmethod
    def _main(cmd: str, home) -> int:
        import json
        import os
        import subprocess
        import sys as _sys

        env = dict(os.environ)
        env["HOME"] = str(home)
        script = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "hooks"
            / "destructive_command_guard.py"
        )
        return subprocess.run(
            [_sys.executable, str(script)],
            input=json.dumps({"tool_input": {"command": cmd}, "tool_name": "Bash"}),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        ).returncode

    @pytest.mark.parametrize(
        "label,cmd",
        [
            ("after if", 'if sh -c "{RM} -rf /a/b"; then :; fi'),
            ("after while", 'while sh -c "{RM} -rf /a/b"; do break; done'),
            ("after until", 'until sh -c "{RM} -rf /a/b"; do break; done'),
            ("after exec", 'exec sh -c "{RM} -rf /a/b"'),
            ("substitution", "$(eval '{RM} -rf /a/b')"),
            ("assignment prefix", 'FOO=1 sh -c "{RM} -rf /a/b"'),
            ("env prefix", 'env sh -c "{RM} -rf /a/b"'),
            ("sudo prefix", 'sudo sh -c "{RM} -rf /a/b"'),
            ("timeout prefix", 'timeout 5 sh -c "{RM} -rf /a/b"'),
        ],
    )
    def test_the_resolver_sees_command_positions_a_name_list_cannot(
        self, label, cmd, tmp_path
    ):
        """Every one of these was ALLOWED while the direct spelling BLOCKED.

        They are not a longer list of names — they are what a real parse gives
        for free. A name-based position test was wrong in BOTH directions at
        once: it under-blocked all of the above and over-blocked
        `echo then bash rm`. That is why the resolver is primary now.
        """
        assert self._main(cmd.format(RM=self.RM), tmp_path) == 2, label

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo then bash {RM}",
            "echo do sh {RM} -rf",
            "echo else bash {RM}",
            "echo elif sh {RM}",
        ],
    )
    def test_a_control_keyword_in_ARGUMENT_position_is_not_a_command_opener(
        self, cmd, tmp_path
    ):
        """The other direction of the same defect.

        A revision that fixed the `time`/`nice` over-block left `then`/`do`/
        `else`/`elif` doing exactly the same thing, and carried a comment
        asserting they could not — MEASURED false: `echo then bash rm` was
        REFUSED while main allowed it.
        """
        assert self._main(cmd.format(RM=self.RM), tmp_path) == 0, cmd

    def test_an_unreadable_command_is_REFUSED_not_degraded(self, tmp_path):
        """The one-character bypass of the mechanism this guard is named for.

        A blind parse used to fall through to `_RM_RF_PATTERN`, which needs a
        GLUED `-rf`. So one trailing quote flipped the verdict:

            eval 'rm -r -f /a/b'        BLOCK
            eval 'rm -r -f /a/b' "      allow      <- same command

        A refusal conditional on the tokenizer succeeding is a refusal the
        caller controls.

        SCOPED TO A LAUNCHER MENTION — every command here names one. The
        unscoped version of this refusal cost 77 of 83,201 recorded commands
        against origin/main and ~91% of those were heredoc scripts the parser
        merely could not read; requiring a launcher costs 53 and keeps every
        measured attack spelling closed. Its other direction is pinned by
        `test_an_unreadable_command_with_NO_launcher_is_left_as_main_has_it`.
        """
        q = chr(34)
        assert self._main(f"eval '{self.RM} -r -f /a/b'", tmp_path) == 2
        assert self._main(f"eval '{self.RM} -r -f /a/b' {q}", tmp_path) == 2
        assert self._main(f"bash -c '{self.RM} -r -f /a/b' {q}", tmp_path) == 2

    def test_an_unreadable_command_with_NO_launcher_is_left_as_main_has_it(
        self, tmp_path
    ):
        """The PRICE of scoping the blind refusal, pinned so it is not a surprise.

        `rm -r -f /a/b "` is unreadable, names a removal, and names no
        launcher. It is ALLOWED — MEASURED identical on origin/main, so this
        change neither opens nor closes it, and the bidirectional corpus sweep
        reports 0 of 83,201 commands going from refused to allowed.

        Kept as an explicit test rather than left implicit, because a NARROWING
        regresses in the direction nobody sweeps: the 153-cell axis sweep only
        exercises CARRIED spellings and is structurally blind to this one.
        """
        q = chr(34)
        assert self._main(f"{self.RM} -r -f /a/b {q}", tmp_path) == 0

        # The discriminator: the SAME unreadable command, plus a launcher.
        assert self._main(f"eval '{self.RM} -r -f /a/b' {q}", tmp_path) == 2

        # And a glued `-rf` still blocks unreadable, via the legacy pattern —
        # so the scoping did not delete the fallback it sits in front of.
        assert self._main(f"{self.RM} -rf /a/b {q}", tmp_path) == 2

    @pytest.mark.parametrize(
        "payload,why",
        [
            ("{RM} -rf /a/b", "shallow — blocked directly too"),
            ("{RM} -rf node_modules", "depth 1 — blocked directly too"),
            ("{RM} -rf ./deep/a/b/c", "deep enough — allowed directly too"),
            ("echo hi", "no removal at all"),
        ],
    )
    def test_a_shell_payload_gets_the_SAME_verdict_as_the_direct_spelling(
        self, payload, why, tmp_path
    ):
        """Shells are RECOVERED, not refused blind — and that is the property.

        `analyze_checked('bash -c "rm -rf /etc"')` returns exes=['bash','rm'],
        so the payload is recoverable and the ordinary operand rules apply to
        it. A previous revision refused all six shell names outright and told
        the reader their payload "cannot be recovered", which was untrue and
        cost ordinary commands like `bash -c 'rm -rf node_modules'`.

        The contract is PARITY, not permissiveness: whatever the direct
        spelling does, the carried spelling does.
        """
        direct = self._main(payload.format(RM=self.RM), tmp_path)
        carried = self._main(f"bash -c '{payload.format(RM=self.RM)}'", tmp_path)
        assert carried == direct, f"{why}: direct={direct} carried={carried}"

    def test_an_UNMODELLABLE_carrier_is_still_refused(self, tmp_path):
        """The shells move to recovery; the fourteen the resolver refuses to
        model do not. `eval` has no grammar to model at all."""
        assert self._main(f"eval '{self.RM} -rf /a/b'", tmp_path) == 2
        assert self._main(f"su -c '{self.RM} -rf /a/b' root", tmp_path) == 2

    def test_the_degraded_path_is_the_PREVIOUS_behaviour_not_a_weaker_one(self):
        """If the resolver cannot be imported, the name-list scan still runs.

        The guarded import's except-path must not be permissive — that is the
        whole answer to "an import adds a failure path to a blocking guard".
        """
        assert _blocks(f"eval '{self.RM} -rf /a/b'"), (
            "with carrier refusal ON (the degraded default) a carrier must block"
        )
        assert dg._analyze_checked is None or dg._UNMODELLABLE_CARRIERS, (
            "the unmodellable set must be non-empty when the resolver is present"
        )

    def test_the_depth_bound_is_gone(self):
        """No numeric bound remains, so no depth can be an attacker's budget."""
        assert not hasattr(dg, "_MAX_CARRIER_DEPTH")

    @pytest.mark.parametrize(
        "cmd",
        [
            "which eval",
            "grep -rn eval src/",
            "echo 'eval is a builtin'",
            "ls /bin/sh",
            "ls -la /usr/bin/script",
            "cp /bin/sh /tmp/x",
            "find / -name sg -delete",
        ],
    )
    def test_a_carrier_NAMED_but_not_RUN_is_untouched(self, cmd):
        """A carrier name in ARGUMENT position launches nothing.

        Every one of these was refused before the command-position scope was
        added — `ls /bin/sh` resolves basename `sh`, which is in the set.
        """
        assert not _blocks(cmd), cmd

    def test_the_carrier_set_covers_every_launcher_the_resolver_names(self):
        """Superset parity: this module keeps a LOCAL copy because it is
        stdlib-only by declaration and must not import the resolver. The copy is
        locked here, so a launcher added to the resolver fails this test until
        it is added there too."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "sp_parity",
            Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "shell_parse.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["sp_parity"] = mod
        spec.loader.exec_module(mod)
        missing = (set(mod._REPARSE_CARRIERS) | set(mod._NESTED)) - set(
            dg._COMMAND_CARRIERS
        )
        assert not missing, f"resolver names launchers this guard does not: {missing}"


class TestCarriedRmdirIsNotThisGuardsBusiness:
    """`rmdir` removes an EMPTY directory and has no recursive-force form.

    The carrier pre-pass refuses without reading the payload, so it inherited
    whatever `_RM_WORD` admitted — and `_RM_WORD` names both verbs because the
    ORDINARY operand scan wants both. MEASURED before the split:

        rmdir /tmp/empty-dir            exit 0
        eval 'rmdir /tmp/empty-dir'     exit 2      <- same command, carried

    That asymmetry is the thing the carrier design exists to remove, not to
    create. Protected-path coverage for `rmdir` is untouched — it lives in
    `protected_paths_guard`, whose own prefilter still names both verbs.
    """

    RM = "r" + "m"

    @staticmethod
    def _main(cmd: str, home) -> int:
        import json
        import os
        import subprocess
        import sys as _sys

        env = dict(os.environ)
        env["HOME"] = str(home)
        script = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "hooks"
            / "destructive_command_guard.py"
        )
        return subprocess.run(
            [_sys.executable, str(script)],
            input=json.dumps({"tool_input": {"command": cmd}, "tool_name": "Bash"}),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        ).returncode

    @pytest.mark.parametrize(
        "cmd",
        [
            "eval '{RM}dir /tmp/empty-dir'",
            "su ubuntu -c '{RM}dir /tmp/empty-dir'",
            "eval {RM}dir /tmp/empty-dir",
        ],
    )
    def test_a_carried_rmdir_is_not_refused(self, cmd, tmp_path):
        """PARITY with the direct spelling, which this guard also allows."""
        direct = self._main(f"{self.RM}dir /tmp/empty-dir", tmp_path)
        assert direct == 0, "precondition: the direct spelling must allow"
        assert self._main(cmd.format(RM=self.RM), tmp_path) == 0, cmd

    def test_a_carried_rm_is_still_refused(self, tmp_path):
        """The control. Narrowing the carrier prefilter must not reach `rm`."""
        assert self._main(f"eval '{self.RM} -rf /a/b'", tmp_path) == 2

    def test_rmdir_beside_an_rm_still_refuses(self, tmp_path):
        """A command naming BOTH verbs still satisfies the narrowed prefilter."""
        assert (
            self._main(f"eval '{self.RM}dir /tmp/x; {self.RM} -rf /a/b'", tmp_path) == 2
        )


class TestACarrierRefusalSaysTheWholeCommandWentToo:
    """A refusal on step 3 discards steps 1 and 2, and must say so.

    Claude Code throws away the ENTIRE Bash call when a PreToolUse hook exits 2.
    Every other exit-2 path in this guard prints the discarded-steps note; the
    carrier pre-pass was added without it, so a caller reading a message about a
    launcher had no reason to suspect an earlier write never happened.

    ⚠ THE EXISTING AST LOCK CANNOT CATCH THIS, which is why the test is
    behavioural. `test_every_configured_python_bash_blocker_emits_the_note`
    asserts the FILE calls `warn()` somewhere — and it does, at the violations
    branch — so a new exit-2 path that skips the call passes it unchanged. A
    grep proves a call exists; only running the guard proves this path reaches
    it.
    """

    RM = "r" + "m"
    MARKER = "the ENTIRE command was discarded"

    @staticmethod
    def _run(cmd: str, home):
        import json
        import os
        import subprocess
        import sys as _sys

        env = dict(os.environ)
        env["HOME"] = str(home)
        script = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "hooks"
            / "destructive_command_guard.py"
        )
        return subprocess.run(
            [_sys.executable, str(script)],
            input=json.dumps({"tool_input": {"command": cmd}, "tool_name": "Bash"}),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_a_multi_step_carrier_refusal_prints_the_note(self, tmp_path):
        cmd = f"echo one > /tmp/step1.txt && eval '{self.RM} -rf /a/b'"
        res = self._run(cmd, tmp_path)
        assert res.returncode == 2, "precondition: the carrier branch must refuse"
        assert self.MARKER in res.stderr, (
            "a carrier refusal discarded an earlier write silently:\n" + res.stderr
        )

    def test_an_unreadable_carrier_refusal_prints_the_note(self, tmp_path):
        """The blind branch is a second exit-2 path through the same call site."""
        q = chr(34)
        cmd = f"echo one > /tmp/step1.txt && eval '{self.RM} -r -f /a/b' {q}"
        res = self._run(cmd, tmp_path)
        assert res.returncode == 2, "precondition: the blind branch must refuse"
        assert self.MARKER in res.stderr, res.stderr

    def test_a_SINGLE_step_carrier_refusal_does_not_print_it(self, tmp_path):
        """The negative control. A one-step command discarded nothing else, and
        a note that fires on every refusal teaches the reader to skip it."""
        res = self._run(f"eval '{self.RM} -rf /a/b'", tmp_path)
        assert res.returncode == 2
        assert self.MARKER not in res.stderr, (
            "the note fired on a single-step command:\n" + res.stderr
        )


class TestUnresolvedVariableIsItsOwnVerdict:
    """#2233: an unexpanded shell variable counted as ONE path component made
    the depth floor positional — ``$SP/head2`` refused at depth 2 while
    ``$SP/a/b/c/d`` passed, same cause, opposite verdict. The verdict must be
    uniform over the cause, not over how many literals follow the variable.
    """

    @pytest.mark.parametrize(
        "target",
        [
            '"$SP/head2"',  # was refused at depth 2 — for the wrong reason
            '"$SP/a/b/c/d"',  # was ALLOWED: the positional hole
            '"$EMPTY/a/b/c/d"',  # empty var → /a/b/c/d
            '"${SP}/x/y/z/w"',  # ${…} spelling of the same cause
            '"$(pwd)/a/b/c"',  # command substitution — equally unresolvable
            '"`printf / x/y/z/w`"',  # backtick substitution: no '$', same hole
            # — the static text has four components but bash evaluates the
            # substitution to '/', so the depth floor never saw the real path
            "'$SP/a/b/c/d'",  # SINGLE-QUOTED: literal to bash, still refused —
            # quote syntax is stripped before the operand is seen, so the
            # guard cannot know the literal is safe; refusing is the honest
            # verdict, and every '$' reaching here is refused for the same
            # reason an unquoted one is.
        ],
    )
    def test_every_unresolved_spelling_is_refused(self, target):
        assert _blocks(f"rm -rf {target}"), (
            f"{target}: an unresolved expansion must not depth-pass on its "
            "literal tail"
        )

    def test_the_reason_names_the_cause(self):
        reasons = dg._rm_violations('rm -rf "$SP/head2"')
        assert any("unresolved shell variable" in r for r in reasons), reasons

    def test_env_expansion_is_not_guessed(self, monkeypatch):
        # expandvars is deliberately NOT applied: the hook's environment is
        # not the shell's, quote context is already stripped, and an unquoted
        # expansion is field-split by bash into operands this function cannot
        # see. Every surviving '$' is refused, resolvable or not — including
        # an env value that would smuggle a shallow path inside a deep one.
        monkeypatch.setenv("DG_TEST_DEEP", "/srv/app/data/build")
        assert _blocks('rm -rf "$DG_TEST_DEEP"')
        monkeypatch.setenv("DG_TEST_SPLIT", "/home /tmp/a/b/c/d")
        assert _blocks('rm -rf $DG_TEST_SPLIT')

    def test_the_fully_literal_deep_path_is_still_allowed(self):
        # The acceptance bar's other direction: routine cleanup under a deep
        # scratch directory must not start failing.
        assert not _blocks("rm -rf /srv/app/data/build")


class TestAResolverThatRAISESKeepsTheFallbackRefusalOn:
    """"The import worked" is not "the resolver answered for THIS command".

    An earlier spelling set the fallback's carrier refusal from
    `_analyze_checked is not None` — a fact about the IMPORT. The two differ on
    exactly one path, the resolver raising, and on that path the earlier
    spelling turned the token-level carrier refusal OFF: it disabled the last
    thing left to catch a carrier precisely when the first thing had failed.

    These call `main()` in-process, because the distinction lives in a branch
    that a subprocess cannot reach without breaking the resolver on disk.
    """

    RM = "r" + "m"

    @staticmethod
    def _main_with(monkeypatch, cmd: str, resolver) -> int:
        import io
        import json

        monkeypatch.setattr(dg, "_analyze_checked", resolver, raising=False)
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps({"tool_input": {"command": cmd}, "tool_name": "Bash"})),
        )
        return dg.main()

    # A carrier the RAW-TEXT scope test cannot see, because shell quoting splits
    # the name — `\bsh\b` and `\beval\b` both miss these, while shlex resolves
    # them to `bash` and `eval`. That combination is the only place the flag
    # changes anything, so a test using a plainly-spelled `eval` proves nothing:
    # the text test refuses it one branch earlier. MEASURED — such a test passed
    # against a mutant that reverted this fix.
    INVISIBLE = [
        ('ba' + chr(39) + 's' + chr(39) + 'h -c', "quote-split bash"),
        ('e' + chr(34) + 'v' + chr(34) + 'al', "quote-split eval"),
        ('ev' + chr(34) + chr(34) + 'al', "empty-string-split eval"),
    ]

    @pytest.mark.parametrize("carrier,why", INVISIBLE)
    def test_a_raising_resolver_still_refuses_a_carrier(
        self, carrier, why, monkeypatch
    ):
        def boom(_cmd):
            raise RuntimeError("resolver exploded")

        cmd = f"{carrier} '{self.RM} -rf /a/b'"
        assert self._main_with(monkeypatch, cmd, boom) == 2, (
            f"{why}: a raising resolver disabled the fallback carrier refusal — "
            "the one path where nothing else is left to catch a carrier"
        )

    @pytest.mark.parametrize("carrier,why", INVISIBLE)
    def test_the_raw_text_scope_really_is_blind_to_these(self, carrier, why):
        """Guard-the-guard: if `_CARRIER_WORDS` saw these, the tests above would
        pass through the text branch and pin nothing."""
        cmd = f"{carrier} '{self.RM} -rf /a/b'"
        assert not dg._CARRIER_WORDS.search(cmd), (
            f"{why}: fixture lost its property — the text scope now matches, so "
            "the raise-path test no longer reaches the flag it claims to pin"
        )

    def test_a_raising_resolver_does_not_refuse_a_plain_safe_command(
        self, monkeypatch
    ):
        """The negative control. Failing closed is not failing on everything."""

        def boom(_cmd):
            raise RuntimeError("resolver exploded")

        cmd = f"{self.RM} -rf ./deep/a/b/c/d"
        assert self._main_with(monkeypatch, cmd, boom) == 0

    def test_a_working_resolver_leaves_the_fallback_refusal_OFF(self, monkeypatch):
        """The other side: with a live resolver the fallback must NOT double up,
        or `bash -c 'rm -rf ./deep/a/b/c/d'` is refused though it is recoverable
        and its direct spelling allows."""
        cmd = f"bash -c '{self.RM} -rf ./deep/a/b/c/d'"
        assert self._main_with(monkeypatch, cmd, dg._analyze_checked) == 0

    def test_a_MISSING_resolver_also_keeps_the_refusal_on(self, monkeypatch):
        """The degraded import path reports unanalysed for the same reason."""
        cmd = f"eval '{self.RM} -rf /a/b'"
        assert self._main_with(monkeypatch, cmd, None) == 2
