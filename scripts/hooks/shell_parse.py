#!/usr/bin/env python3
"""Shared shell-command analysis for guard hooks.

A security guard must classify what a Bash command ACTUALLY executes — its real
subcommands and flags — not what a substring or a naive regex suggests. The
naive approaches fail on, e.g.:

* a commit message that merely *mentions* ``git push`` (must NOT match),
* a quoted flag ``git commit '--no-verify'`` that the shell still passes (MUST
  match),
* a wrapper prefix ``sudo git push`` / ``env X=1 git push`` / ``/usr/bin/git
  push`` (MUST match),
* a nested script ``bash -c 'git commit -n …'`` (the inner command MUST be
  seen),
* a leading redirect ``git 2>/dev/null push`` / ``git 2>&1 commit`` whose
  operator must NOT be mistaken for the subcommand (the push/commit gates MUST
  still match — a leaking redirect once let these slip),
* an approval comment ``# review-override`` that belongs to ONE command segment
  and must not authorize the next.

This module centralizes that parsing so ``git_push_guard``,
``review_enforcement_commit``, and the destructive/path guards agree. Stdlib
only; fail-open (a segment that won't tokenize degrades to a naive split rather
than raising) — a guard must never crash the tool.

That fail-open degradation is SILENT by design, which means ``analyze()`` can
never report its own blind spot: "no gated segment found" and "no gated command
present" are indistinguishable in its return value. A caller that treats the
former as the latter fails OPEN. ``untokenizable()`` exists so a
security-critical caller can tell them apart and choose its own fail direction
at its own boundary — the parser degrades gracefully, each gate decides for
itself what an unverifiable command means. Callers must probe the RAW command:
normalizing text before a blind-spot probe can only ever delete the evidence
the probe looks for.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import NamedTuple

# Leading wrappers whose trailing arguments are the real command to inspect.
# Per wrapper: (option flags that consume a following value token, count of bare
# positional args that precede the command). Lets `timeout 5 git push`,
# `sudo -u root git push`, `nice -n 10 git push` resolve to the real executable.
_WRAPPER_SPEC = {
    "sudo": (
        {
            "-u",
            "-g",
            "-p",
            "-C",
            "-U",
            "-R",
            "-h",
            "-t",
            "--user",
            "--group",
            "--prompt",
            "--chdir",
            "--close-from",
            "--role",
            "--type",
        },
        0,
    ),
    "doas": ({"-u", "-C"}, 0),
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "--class", "-n", "--classdata", "-p", "--pid"}, 0),
    "chrt": (set(), 1),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "nohup": (set(), 0),
    "setsid": (set(), 0),
    "time": ({"-o", "--output", "-f", "--format"}, 0),
    "command": (set(), 0),
    "exec": ({"-a"}, 0),
    "xargs": (
        {
            "-I",
            "-i",
            "-n",
            "--max-args",
            "-P",
            "--max-procs",
            "-s",
            "--max-chars",
            "-E",
            "-L",
            "--max-lines",
            "-d",
            "--delimiter",
            "-a",
            "--arg-file",
            "-e",
            "--eof",
            "--replace",
        },
        0,
    ),
}
_WRAPPERS = set(_WRAPPER_SPEC)
# Interpreters that run a script string passed after -c; recurse into it.
_NESTED = {"bash", "sh", "dash", "zsh", "ksh", "ash"}
# Shell tokens that can front a SIMPLE COMMAND within a segment (after
# split_segments has already cut on ; | & && || newline). Stripping them at
# command position lets analyze() resolve the real exe THROUGH a control
# structure or group — `if …; then git clean -f; fi`, `while …; do …`,
# `! git clean`, `{ git clean -f; }` — so a gate that keys on `seg.exe == git`
# is not silently skipped. EXCLUDES `for/case/select/in` (they front a WORD, not
# a command: `for x in a b`) and all block CLOSERS (`fi done esac } )` — never
# precede a command). Group opener `(` (bare and GLUED, `(git`) is handled
# structurally in _strip_wrappers, not via this set.
_CMD_POSITION_WORDS = frozenset({"!", "if", "elif", "while", "until", "then", "do", "else", "{"})
# git global options that consume the FOLLOWING token as their value.
_GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
# git-commit short flags that consume the REST of their short-bundle as a value
# (so -minitial is `-m initial`, not a bundle containing -n).
_COMMIT_ARG_FLAGS = "mFCc"


@dataclass
class Segment:
    """One executed command segment."""

    exe: str  # resolved executable basename (e.g. "git", "gh"), "" if unknown
    argv: list[str]  # argv with wrappers/env-assignments stripped
    override: bool  # a trailing `# review-override` shell comment on this segment
    raw: str  # the raw segment text (for messages)
    depth: int = 0  # 0 = top level, >0 = inside a sh -c script
    redirects: list[str] = field(
        default_factory=list
    )  # expansion redirect targets excised from argv


def _redirect_operator_len(command: str, i: int) -> int | None:
    """Length of a shell REDIRECT operator starting at ``command[i]``, else None.

    The closed bash redirect grammar (operator only — a leading fd digit is
    already buffered and the following target is consumed by the caller):
    ``>`` ``>>`` ``>&`` ``>|`` ``<`` ``<<`` ``<<<`` ``<&`` ``&>`` ``&>>``.
    A bare ``&`` (background) and a bare ``|`` (pipe) are NOT redirects — they
    stay control operators. Process substitution ``>(…)``/``<(…)`` is
    deliberately NOT treated as a redirect (see ``_substitutions``' documented
    gap); ``>(`` returns length 1 here so the ``(`` is handled normally.
    """
    n = len(command)
    c = command[i]
    if c == "&":  # &> / &>> only — a lone '&' is the background control operator
        if i + 1 < n and command[i + 1] == ">":
            return 3 if (i + 2 < n and command[i + 2] == ">") else 2
        return None
    if c == ">":
        nxt = command[i + 1] if i + 1 < n else ""
        return 2 if nxt in (">", "&", "|") else 1
    if c == "<":
        if command[i + 1 : i + 3] == "<<":  # <<<
            return 3
        nxt = command[i + 1] if i + 1 < n else ""
        return 2 if nxt in ("<", "&") else 1
    return None


_TARGET_STOP = (" ", "\t", "\n", ";", "|", "&", "<", ">", "(", ")")


class _ParsedSegment(NamedTuple):
    """A split segment with two text views.

    ``raw`` is byte-identical to what ``split_segments`` has always returned (the
    executed-segment string, comment retained; an EXPANSION-carrying redirect target
    is retained so ``_substitutions`` still sees the nested command it EXECUTES).
    ``argv_src`` is ``raw`` with every redirect operator+target removed — the string
    ``analyze`` tokenizes into argv, so a redirect target can never spoof the
    subcommand. ``redirects`` are the expansion operator-target words excised from
    ``argv_src`` (observability).
    """

    raw: str
    argv_src: str
    redirects: tuple[str, ...]


def _command_sub_end(command: str, i: int, n: int) -> int:
    """Index just past the matching ``)`` of a ``$(…)`` command substitution that
    opens at ``command[i:i+2] == "$("``.

    QUOTE-, ESCAPE-, and NESTING-aware: a ``)`` inside a single/double-quoted span,
    or backslash-escaped, is DATA — it does NOT close the substitution; a nested
    ``$(…)``/``(…)`` bumps the paren depth; a ``` `…` ``` backtick span is skipped
    whole. This bounds the sub at its TRUE close so a following control operator
    (``&& rm``) can never be swallowed into a redirect target (the Codex-P1
    regression a paren-only balancer caused). Fail-open: an UNTERMINATED sub returns
    ``n`` (consume to EOL) — it never raises (the module's no-crash contract).

    Shared by ``_redirect_target_end`` (target boundary) and ``_substitutions``
    (body extraction) so the two agree on where a ``$()`` ends — one scanner, not
    two divergent paren-counters.
    """
    depth, j = 1, i + 2
    q: str | None = None  # in-body quote state
    while j < n:
        ch = command[j]
        if q is not None:
            if q == '"' and ch == "\\" and j + 1 < n:  # \ escapes only inside "…"
                j += 2
                continue
            if ch == q:
                q = None
            j += 1
            continue
        if ch == "\\" and j + 1 < n:  # unquoted backslash escapes the next char
            j += 2
            continue
        if ch in ("'", '"'):  # a quoted span begins — its inner ) is data
            q = ch
            j += 1
            continue
        if ch == "`":  # nested backtick span — skip to its close (or EOL)
            close = command.find("`", j + 1)
            j = close + 1 if close != -1 else n
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n  # unterminated — fail-open to EOL, never raise


def _redirect_target_end(command: str, j0: int, n: int) -> int:
    """Index just past ONE complete redirect-target word starting at ``command[j0]``.

    Bash word grammar for a redirect target: a run ended only by an UNQUOTED,
    UNESCAPED metacharacter (whitespace or one of ``; | & < > ( )``). A backslash
    escapes the next char; single/double quotes open a span that suppresses
    metacharacters (``\\`` active only inside double quotes); a ``$(…)`` command
    substitution (bounded QUOTE/ESCAPE/NESTING-aware via ``_command_sub_end`` — a
    ``)`` inside a quoted operand does NOT close it) and a ``` `…` ``` backtick
    substitution are PART of the word (their inner ``(``/``)`` and spaces do NOT end
    it). This is BOUNDARY detection only — expansion SEMANTICS stay with the
    canonical ``_substitutions`` parser. It generalises the earlier quote-aware
    scanner (which stopped at a bare ``(``) so an UNQUOTED ``2>$(rm x)`` is measured
    as one word and excised from argv whole, not just its leading ``$``.
    """
    j = j0
    wq: str | None = None  # in-word quote state
    while j < n:
        ch = command[j]
        if wq is not None:
            if wq == '"' and ch == "\\" and j + 1 < n:
                j += 2
                continue
            if ch == wq:
                wq = None
            j += 1
            continue
        if ch == "\\" and j + 1 < n:  # unquoted backslash escapes the next char
            j += 2
            continue
        if ch in ("'", '"'):  # a quote span begins (or concatenates)
            wq = ch
            j += 1
            continue
        if ch == "$" and j + 1 < n and command[j + 1] == "(":  # $(…) command sub
            j = _command_sub_end(command, j, n)  # quote/escape/nesting-aware close
            continue
        if ch == "`":  # `…` backtick sub — to the matching backtick (or EOL)
            close = command.find("`", j + 1)
            j = close + 1 if close != -1 else n
            continue
        if ch in _TARGET_STOP:  # unquoted metacharacter ends the word
            break
        j += 1
    return j


def parse_segments(command: str) -> list[_ParsedSegment]:
    """Split a command line into executed segments, returning per segment BOTH the raw
    text and a redirect-STRIPPED argv source (see ``_ParsedSegment``).

    Quote-aware: an operator inside a quoted string does not split. Splits on ``&&``,
    ``||``, ``;``, ``|``, ``&`` and newlines. A ``#`` comment (opened outside quotes)
    runs to end-of-line and is retained in ``raw`` so override detection can see it.

    Redirect-aware: a redirection (``2>/dev/null``, ``> out.log``, ``2>&1``, ``&>log``,
    ``>| f``, ``< in``, ``<<<here``) is consumed — operator AND target. A PLAIN-filename
    target is dropped from BOTH views. A target that can carry a command expansion (any
    ``$`` or backtick — ``2>$(rm x)``, ``2>"$(rm x)"``, ``2>$VAR``, backtick) is KEPT in
    ``raw`` (so ``_substitutions`` still sees the nested command a substitution redirect
    target EXECUTES) but EXCLUDED from ``argv_src`` — so it can no longer leak into argv
    and spoof ``git_subcommand``/``commit_skips_hooks`` (the push/commit fail-open this
    split closes). Process substitution ``<(…)``/``>(…)`` stays in BOTH (documented gap).
    ``raw`` stays byte-identical to the historical ``split_segments`` output for every
    ordinary command (the cwd/occurrence consumers match ``Segment.raw`` against a fresh
    re-split; locked by a golden test over a broad corpus). ONE intentional difference
    from the pre-#1455 scanner: a ``$(…)``/backtick target that contains an UNQUOTED
    control operator (``;`` ``&&`` ``||`` ``|``) is now paren-balanced into ONE segment
    instead of mis-split on that operator — which CLOSES an additional fail-open (HEAD
    mis-split ``git 2>$(a; b) push`` into ``git  $(a`` + ``b) push`` so ``git_subcommand``
    never saw the ``push``); the nested ``a``/``b`` still surface via ``_substitutions``.
    So ``raw`` is byte-identical EXCEPT it is strictly SAFER on this class — never the
    other direction (locked by the ``$(;)``/``$(&&)``/``$(|)`` cases in the redirect-argv
    test's EXPLOITS).
    """
    pairs: list[tuple[str, str, list[str]]] = []
    raw_buf: list[str] = []
    argv_buf: list[str] = []
    redirs: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        c = command[i]
        if quote:
            raw_buf.append(c)
            argv_buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                raw_buf.append(command[i + 1])
                argv_buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            raw_buf.append(c)
            argv_buf.append(c)
            i += 1
            continue
        two = command[i : i + 2]
        if two in ("&&", "||"):
            pairs.append(("".join(raw_buf), "".join(argv_buf), list(redirs)))
            raw_buf, argv_buf, redirs = [], [], []
            i += 2
            continue
        op_len = _redirect_operator_len(command, i)
        if op_len is not None:
            # Drop a standalone leading fd digit-run from BOTH buffers (the '2' of
            # ` 2>`), but NOT a digit that ends a word (`push2>x` keeps 'push2'). The
            # trailing digits are mirrored in both buffers, so remove the same count.
            if c != "&":
                k = len(raw_buf)
                while k > 0 and raw_buf[k - 1].isdigit():
                    k -= 1
                if k < len(raw_buf) and (k == 0 or raw_buf[k - 1].isspace()):
                    ndel = len(raw_buf) - k
                    del raw_buf[k:]
                    del argv_buf[len(argv_buf) - ndel :]
            j = i + op_len
            while j < n and command[j] in (" ", "\t"):  # gap before the target
                j += 1
            t = command[j] if j < n else ""
            tnext = command[j + 1] if j + 1 < n else ""
            # A process substitution as the target (`<(…)`, `>(…)`) begins with a
            # metachar the plain consumer would stop on — leave it in BOTH views (it
            # executes, like any substitution); consume only the operator.
            if t in ("<", ">") and tnext == "(":
                raw_buf.append(" ")
                argv_buf.append(" ")
                i = j
                continue
            j0 = j
            if j < n and t not in _TARGET_STOP:
                j = _redirect_target_end(command, j0, n)
            target = command[j0:j]
            if "$" in target or "`" in target:
                # Expansion-carrying target: KEEP in raw (nested command stays visible
                # to the destructive guard via _substitutions), EXCLUDE from argv_src so
                # it cannot spoof the subcommand. Record it for observability.
                raw_buf.append(" ")
                raw_buf.append(target)
                argv_buf.append(" ")
                redirs.append(target)
            else:  # plain filename target — dropped from both views
                raw_buf.append(" ")
                argv_buf.append(" ")
            i = j
            continue
        if c in (";", "|", "&", "\n"):
            pairs.append(("".join(raw_buf), "".join(argv_buf), list(redirs)))
            raw_buf, argv_buf, redirs = [], [], []
            i += 1
            continue
        raw_buf.append(c)
        argv_buf.append(c)
        i += 1
    pairs.append(("".join(raw_buf), "".join(argv_buf), list(redirs)))
    return [
        _ParsedSegment(raw=r.strip(), argv_src=a.strip(), redirects=tuple(d))
        for (r, a, d) in pairs
        if r.strip()  # filter on RAW — keeps the exact set/alignment split_segments had
    ]


def split_segments(command: str) -> list[str]:
    """Executed-segment raw strings — a thin, byte-identical view over
    ``parse_segments`` (all redirect/quote/comment semantics live there). Kept as the
    stable ``list[str]`` API the cwd/occurrence consumers iterate."""
    return [p.raw for p in parse_segments(command)]


def has_top_level_pipe(command: str, *, count_substitutions: bool = False) -> bool:
    """Whether *command* contains a real top-level shell PIPE (``a | b``).

    Quote-, redirect-, and substitution-aware: a ``|`` inside a quoted string (a jq
    program, ``grep -F '|'``), a ``||`` control operator, a ``>|`` redirect operator,
    or a command substitution ``$( … )`` / ``` `…` ``` (whose output is CAPTURED, not
    streamed to the swallowed background stdout — ``RESULT=$(cmd | filter)``) is NOT a
    background pipe. A bare subshell ``(cmd | x)`` DOES stream to the background stdout,
    so its ``|`` still counts. Used by the run_in_background guard: a piped background
    command's stdout is swallowed, so ONLY a genuine streamed pipe should block it.

    Residual (accepted for a CONVENIENCE guard — friction, not a sandbox): the quote
    model still mirrors ``split_segments``, so a quote that is backslash-escaped OUTSIDE
    quotes (``printf %s \\"foo | cat``), a stray quote char in a ``#`` comment or a
    ``<<EOF`` heredoc body, or a ``|`` in a ``case`` pattern can MISread — OVER-reading
    (``case`` ``|`` looks like a pipe) or UNDER-reading (a swallowed quote hides a later
    pipe). Never a security bypass either way (an over-read is a reworked command, an
    under-read re-exposes the empty-output footgun this guard usually prevents); closing
    these fully is the unbounded quote-parsing tail shared with ``split_segments``.

    ``count_substitutions`` inverts the ``$( … )`` rule, and exists because the two
    consumers need OPPOSITE answers about the same syntax. The background-output guard
    (default, ``False``) is right to skip them: ``RESULT=$(cmd | filter)`` captures its
    output, so nothing is swallowed. The pipe-STATUS guard needs ``True``: bash defines
    an assignment-only command's status as the status of the command substitution, so
    ``rc=$(prog | tail); echo $?`` reads the FILTER's status — exactly the footgun that
    guard exists to flag, and invisible while substitutions are skipped. Kept as one
    scanner with a flag rather than a second copy: two hand-rolled shell scanners drift,
    and this file's whole purpose is that there is one.
    """
    i, n = 0, len(command)
    quote: str | None = None
    subst_depth = 0  # inside $( … ): its output is CAPTURED, not streamed to bg stdout
    in_backtick = False
    while i < n:
        c = command[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if in_backtick and not count_substitutions:  # `…` output captured: not a bg pipe
            if c == "`":
                in_backtick = False
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            continue
        if c == "`":
            in_backtick = not in_backtick
            i += 1
            continue
        if (
            c == "$" and i + 1 < n and command[i + 1] == "("
        ):  # $( … ) opens a capturing substitution
            if not count_substitutions:
                subst_depth += 1
            i += 2
            continue
        if c == ")" and subst_depth > 0:
            subst_depth -= 1
            i += 1
            continue
        if command[i : i + 2] in ("&&", "||"):  # control operators, not a pipe
            i += 2
            continue
        op_len = _redirect_operator_len(command, i)
        if op_len is not None:  # a redirect operator (incl. ``>|``) — skip it whole
            i += op_len
            continue
        # A `|` inside $()/backtick is captured (not a bg pipe); a bare subshell `(…)`
        # streams to the background stdout, so its `|` DOES count (subst_depth stays 0).
        if c == "|" and subst_depth == 0:
            return True
        i += 1
    return False


def _strip_trailing_comment(seg: str) -> str:
    """Remove an unquoted ``#`` comment (whitespace-preceded or at start) to EOL."""
    out: list[str] = []
    quote: str | None = None
    prev_ws = True  # start-of-string counts as preceding whitespace
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote:
            out.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                out.append(seg[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            prev_ws = False
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            prev_ws = False
            i += 1
            continue
        if c == "#" and prev_ws:
            break  # comment to end of segment
        out.append(c)
        prev_ws = c.isspace()
        i += 1
    return "".join(out)


# The recognized ack/override sigils. A trailing comment may carry several of these
# together (`# audit-ack depth-ack`), so the leading run of the comment is allowed to
# contain any of them; the FIRST prose token ends the run. Kept in sync with the
# sigils actually passed to has_trailing_override across the guard hooks.
_KNOWN_SIGILS = (
    "review-override",
    "depth-ack",
    "audit-ack",
    "escalation-ack",
    "ci-override",
    "stale-review-override",
    "scheduled-review-override",
    "discard-override",
    # Both of these were passed to has_trailing_override from the day they
    # shipped but never listed here, which made the "kept in sync" claim above
    # false and had a MEASURED effect: the leading-run scan treats an unlisted
    # token as PROSE, so an unlisted sigil written FIRST silently disables every
    # sigil after it (`# full-suite-ok audit-ack` → audit-ack undetected). The
    # sigil queried first still matched, which is why it went unnoticed.
    #
    # NOTE this widens acceptance as well as detection, in the fail-OPEN
    # direction, and the reach is wider than the merge gate. MEASURED against the
    # previous parser, every one of these flipped False -> True:
    #   `git clean -fdx  # full-suite-ok discard-override`         -> the
    #   `git clean -fdx  # merge-to-main-override discard-override`   UNRECOVERABLE
    #                                                                 clean block
    #   `gh pr merge …   # full-suite-ok review-override`          -> findings gate
    #   `gh pr merge …   # merge-to-main-override ci-override`     -> CI gate
    #   `git commit      # full-suite-ok audit-ack | depth-ack`    -> commit gates
    # That is the intended contract — the operator typed both sigils literally,
    # and nothing in the repo auto-composes a multi-sigil comment — but it is a
    # gate-loosening change, the sharpest case is the one that deletes untracked
    # files, and both are named here rather than left to be discovered.
    #
    # A test derives this set from the guards themselves (an ast walk over
    # scripts/hooks/), so the next divergence fails a test rather than waiting to
    # be noticed.
    "merge-to-main-override",  # git_push_guard: local `git merge` onto main/master
    "full-suite-ok",  # full_suite_guard: run the whole pytest suite locally
)


def _token_is_sigil(tok: str, sigil: str) -> bool:
    """Whether ``tok`` IS the sigil token — the sigil optionally followed by
    punctuation (``review-override:``), but NOT a prefix of a longer word-token
    (``review-override-x``)."""
    return bool(re.match(re.escape(sigil) + r"(?![-\w])", tok))


def _has_trailing_override(seg: str, sigil: str = "review-override") -> bool:
    """Whether the segment carries a genuine ``# <sigil>`` comment.

    The ``#`` must open a real comment (outside quotes, preceded by whitespace),
    so a token buried in a quoted message word does not count. ``sigil`` selects
    which override token to detect (``review-override`` by default; the CI-status
    merge gate passes ``ci-override``).

    The sigil must appear in the LEADING contiguous run of recognized ack/override
    tokens: `# review-override: accepted P2s` (sigil first, prose follows) and
    `# audit-ack depth-ack` (a run of two sigils — each satisfies its own check)
    both count, but `# not a review-override` / `# see review-override docs` do NOT
    — a prose token ahead of the sigil ends the run. This keeps independent acks
    able to coexist without letting an incidental or negated prose mention waive
    the gate.
    """
    quote: str | None = None
    prev_ws = True
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            prev_ws = False
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            prev_ws = False
            i += 1
            continue
        if c == "#" and prev_ws:
            for tok in seg[i + 1 :].split():
                if _token_is_sigil(tok, sigil):
                    return True  # the queried sigil, reached within the leading run
                if not any(_token_is_sigil(tok, s) for s in _KNOWN_SIGILS):
                    return False  # a prose token ends the leading run of sigils
                # else: a DIFFERENT recognized sigil — still in the run, keep scanning
            return False
        prev_ws = c.isspace()
        i += 1
    return False


def has_trailing_override(seg: str, sigil: str = "review-override") -> bool:
    """Public alias of :func:`_has_trailing_override` for sibling hooks."""
    return _has_trailing_override(seg, sigil)


def _argv(seg: str) -> list[str]:
    """shlex argv of a comment-stripped segment; naive split on tokenizer error."""
    core = _strip_trailing_comment(seg)
    try:
        return shlex.split(core)
    except ValueError:
        return core.split()


def untokenizable(command: str) -> bool:
    """True when ``shlex`` cannot cleanly tokenize the command.

    This is the blind-spot signal a guard consults when it is about to conclude
    "no gated segment found". ``_argv`` degrades to a naive split on the SAME
    ``ValueError`` silently, so ``analyze()`` can never self-report that its
    result is untrustworthy: an ordinary quoting construct is enough to shift
    segmentation off and drop a real, executing command from the parse, and the
    return value looks identical to "there was nothing to find".

    Deliberately reads the WHOLE raw command, with no normalization of any kind.
    An earlier version pre-processed it to suppress prompts on a class of
    multi-line command, and that MEASURABLY disarmed the signal: on a shape a
    developer writes without thinking, the command really ran (verified against
    a shimmed binary, so the proof was execution rather than parse) while the
    pre-processed text tokenized cleanly and the guard fell silent. The
    triggering shape is deliberately not written down — this file is public and
    the guard it protects is load-bearing.

    KNOWN COST, stated rather than hidden: ``_argv`` DOES normalize before its
    own tokenize (it strips trailing comments), so this probe over-reports
    relative to the very parser whose blind spot it reports — an unquoted
    comment alone can make a benign command look unparseable. Stripping here is
    NOT the fix: measured over 19,246 real commands, doing so erases a mention
    of a gated operation in 3 of them, because a stripper's model of where a
    comment begins is not the shell's and the two disagree in both directions.
    A cure that can only raise severity, never clear it, is tracked separately.

    That rule is now literal. An earlier revision folded ``\\<newline>`` to a
    SPACE before probing, which contradicted the paragraph above and was also
    simply wrong about bash — bash REMOVES a line continuation, joining the two
    halves into one word (``ec\\<newline>ho`` runs ``echo``), so replacing it
    with a space produced the reading furthest from what actually executes.
    MEASURED over 12,099 real commands: folding and not folding classify
    IDENTICALLY (339 un-tokenizable either way, zero commands differ), so the
    normalization bought nothing and is removed rather than documented.
    """
    try:
        shlex.split(command)
        return False
    except ValueError:
        return True


def _basename(token: str) -> str:
    """Executable basename: /usr/bin/git → git, ./foo → foo."""
    return token.rsplit("/", 1)[-1]


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Drop leading shell command-position tokens, env-assignments (VAR=x), and
    wrapper commands (sudo/env/…) so the returned argv[0] is the ACTUAL executed
    command, and peel the matching subshell-close `)` off the revealed argv.

    Command-position tokens (`then`/`do`/`!`/`(`/`{` …) can wrap a command inside
    a control structure or subshell; a leading `(` may be GLUED to the command
    (`(git`). Reserved words are stripped UNCONDITIONALLY at the front so the real
    command is revealed through any control wrapper (`time ! git push`,
    `if git push; then …`, `until git push; do …` all resolve to `git push`). A
    leading `(` may nest (`( (cmd) )` spaced); its matching trailing `)` closers
    (as many as `(` openers consumed) are peeled off the operand(s) carrying the
    close.

    Accepted SAFE-DIRECTION residual: a reserved word that is genuinely a command
    NAME rather than a control keyword (`command if git push` — `command` runs an
    executable literally named `if`) is still stripped, so analyze() over-resolves
    to `git push` and the push gate fires on a push bash would not actually run.
    That is an OVER-gate (a spurious block of an exotic, near-never-typed form),
    never a MISS — the monotonic-safe direction. Modelling wrapper-vs-keyword
    precisely was tried (round-2 `wrapper_consumed`) and REMOVED: it turned the
    over-gate into a real false-negative (`time ! git push` → exe `!` → push gate
    MISS), which is the unsafe direction. #1457 round-3.

    Redirection syntax is deliberately NOT touched here: this resolver feeds
    flag/value parsers (`git_subcommand`, `gh_pr_subcommand`, `commit_skips_hooks`)
    and dropping a token would shift positions and let a value-flag swallow the
    real gated token (a fail-closed-gate bypass — #1457 round-2). A consumer that
    reads POSITIONALS skips redirections LOCALLY, inside its own walk and AFTER its
    own flag/value handling (full_suite_guard, protected_paths, destructive each do)
    — never as a pre-pass, which would recreate the same desync.

    Safe-direction / MONOTONIC for security: the arithmetic, command-position and
    `(` branches fire ONLY when argv[0] is `((`-prefixed / a control token /
    `(`-prefixed, so a normal command (argv[0] ∈ {git, gh, rm, …}) resolves to the
    same exe and the same argv and is returned byte-for-byte unchanged — the strip
    can REVEAL a hidden command but never hide a caught one. `((…))` is bash
    ARITHMETIC evaluation, which runs NO external command, so its outer segment
    resolves to nothing (a command hidden in a `$(…)` inside it still surfaces via
    analyze()'s separate substitution path).
    """
    argv = list(argv)  # local copy — we may rebind a glued `(token` / strip closers
    if argv and argv[0].startswith("(("):
        return []  # `((…))` arithmetic evaluation — no external command runs
    i = 0
    open_parens = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("("):  # subshell opener, bare `( git` or glued `(git`
            open_parens += 1
            if tok == "(":
                i += 1
            else:
                argv[i] = tok[1:]  # "(git" -> "git"; reprocess (token strictly shrinks)
            continue
        if tok in _CMD_POSITION_WORDS:
            i += 1  # reserved word / brace-group opener at command position
            continue
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            i += 1  # leading VAR=value assignment
            continue
        spec = _WRAPPER_SPEC.get(_basename(tok))
        if spec is None:
            break
        argflags, positional = spec
        i += 1
        # consume the wrapper's own value-flags and leading positional args
        while i < len(argv):
            t = argv[i]
            if t == "--":
                i += 1
                break
            if t.startswith("-"):
                if t in argflags and "=" not in t:
                    i += 2  # flag + its separate value token
                else:
                    i += 1
                continue
            if "=" in t and t.split("=", 1)[0].isidentifier():
                i += 1
                continue
            if positional > 0:
                positional -= 1
                i += 1
                continue
            break  # this bare word is the wrapped command
    result = list(argv[i:])  # redirections are NOT stripped here (see docstring)
    if open_parens and result:
        # Peel matching trailing `)` closers off the operand(s) carrying the
        # subshell close — up to the number of `(` openers consumed, scanning from
        # the end (a redirection can follow the closer: `(rm -rf /x) 2>/dev/null`).
        # A redirect target that itself ends in `)` (`(cmd) > 'log)'`) is a rare
        # form a POSITIONAL consumer resolves safe-direction via its own local
        # redirection skip (protected_paths/destructive `_REDIR_TOKEN`).
        remaining = open_parens
        j = len(result) - 1
        while remaining > 0 and j >= 0:
            if result[j] == ")":
                del result[j]
                remaining -= 1
                j -= 1
            elif result[j].endswith(")"):
                result[j] = result[j][:-1]
                remaining -= 1  # stay on j — the token may carry another glued `)`
            else:
                j -= 1
    return result


def analyze(command: str) -> list[Segment]:
    """Parse a Bash command into executed Segments (nested scripts flattened).

    Each Segment reports the resolved executable basename, its argv (wrappers
    and env-assignments stripped), and whether a ``# review-override`` comment
    is bound to that segment. ``bash -c 'script'`` is recursed into so the inner
    commands are surfaced (the parent's override propagates to them).
    """
    out: list[Segment] = []
    for seg in parse_segments(command):
        raw = seg.raw
        override = _has_trailing_override(raw)
        # argv is tokenized from the redirect-STRIPPED source, so a redirect target
        # (incl. an expansion one) can never become argv[1] and spoof the subcommand.
        argv = _strip_wrappers(_argv(seg.argv_src))
        exe = _basename(argv[0]) if argv else ""
        out.append(
            Segment(exe=exe, argv=argv, override=override, raw=raw, redirects=list(seg.redirects))
        )
        nested = []
        if exe in _NESTED:
            script = _nested_script(argv)
            if script:
                nested.append(script)
        # $(...) / `...` bodies also execute — parsed from RAW, which STILL carries any
        # expansion redirect target, so a nested command stays visible to the guards.
        nested.extend(_substitutions(raw))
        for script in nested:
            for inner in analyze(script):
                out.append(
                    Segment(
                        exe=inner.exe,
                        argv=inner.argv,
                        override=override or inner.override,
                        raw=inner.raw,
                        depth=inner.depth + 1,
                        redirects=inner.redirects,
                    )
                )
    return out


def is_pytest_invocation(seg: Segment) -> bool:
    """Whether a parsed Segment IS a pytest run (not a mere textual mention).

    True for the ``pytest`` entrypoint (``pytest …``, ``/venv/bin/pytest …``) or a
    python interpreter invoked with ``-m pytest``. Because ``analyze`` splits
    quote-aware, a ``|pytest`` inside a quoted argument — e.g. ``grep 'a|pytest' f``
    — is NOT a pytest segment here, unlike a raw-regex scan of the command string.
    """
    if seg.exe == "pytest":
        return True
    if seg.exe.startswith("python"):
        argv = seg.argv
        i = 1
        while i < len(argv):
            tok = argv[i]
            if tok == "-m":  # `python -m pytest …`
                return i + 1 < len(argv) and argv[i + 1] == "pytest"
            if tok in ("-c", "-W", "-X"):  # flags that consume the next token
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            # First non-flag = the program python runs. Only a pytest console-script
            # entrypoint (a /path/.../pytest) is a pytest run; `python script.py …`
            # is NOT, even if `-m pytest` appears later as the SCRIPT's own args.
            return "/" in tok and _basename(tok) == "pytest"
    return False


def command_runs_pytest(command: str) -> bool:
    """Whether any executed segment of ``command`` is a pytest run (quote-aware)."""
    try:
        return any(is_pytest_invocation(s) for s in analyze(command))
    except Exception:
        return False  # parse failure → fail open (a convenience check, never a gate)


def _substitutions(text: str) -> list[str]:
    """Command-substitution bodies — ``$(…)`` and ``` `…` ``` — which also run.

    Only single-quoted spans block substitution (``$()`` still expands inside
    double quotes). A ``$()`` body is bounded QUOTE/ESCAPE/NESTING-aware via the
    shared ``_command_sub_end`` — a ``)`` inside a quoted operand of the body is
    DATA, so the body is extracted to its TRUE close (the same scanner the redirect-
    target boundary uses, so the two never disagree on where a ``$()`` ends).
    Best-effort: exotic forms (process substitution ``<(…)``, ANSI-C ``$'…'``
    scripts, ``env -S`` string-splitting, shell aliases/functions) are NOT parsed —
    this guard is an approval/friction layer, not a sandbox, and fails toward
    over-matching on the common forms rather than pretending to cover every shell
    construct.
    """
    subs: list[str] = []
    i, n = 0, len(text)
    in_sq = False
    while i < n:
        c = text[i]
        if in_sq:
            if c == "'":
                in_sq = False
            i += 1
            continue
        if c == "'":
            in_sq = True
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            end = _command_sub_end(text, i, n)  # index past matching ')'
            subs.append(text[i + 2 : end - 1])  # body between $( and )
            i = end
            continue
        if c == "`":
            j = text.find("`", i + 1)
            if j != -1:
                subs.append(text[i + 1 : j])
                i = j + 1
                continue
        i += 1
    return subs


def _nested_script(argv: list[str]) -> str:
    """The script string passed to an interpreter's ``-c``, else ''.

    Handles a bare ``-c`` (script is the next token), a combined short bundle
    where ``c`` is last (``-lc 'script'`` → next token), and an inline value
    (``-c'script'`` → the rest of the token after ``c``).
    """
    for i, tok in enumerate(argv[1:], 1):
        if not tok.startswith("-") or tok.startswith("--"):
            continue
        pos = tok.find("c")
        if pos <= 0:
            continue
        if pos == len(tok) - 1:  # 'c' is the last flag in the bundle
            if i + 1 < len(argv):
                return argv[i + 1]
        else:  # inline script glued after the 'c'
            return tok[pos + 1 :]
    return ""


# ── git-specific helpers ────────────────────────────────────────────────


def git_subcommand(argv: list[str]) -> str | None:
    """The git subcommand for an argv whose executable is git, skipping git
    global options (including ``-c KEY=VAL`` / ``-C DIR`` which take a value)."""
    if not argv or _basename(argv[0]) != "git":
        return None
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in _GIT_OPTS_WITH_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return None


def gh_pr_subcommand(argv: list[str]) -> str | None:
    """For a ``gh`` argv, the subcommand after ``pr`` (create/merge/…), else None.

    Scans for the ``pr`` token so a global flag before it
    (``gh --repo o/r pr merge``) does not evade detection. A value-taking flag
    BETWEEN ``pr`` and the subcommand (``gh pr -R o/r merge``) is consumed WITH
    its value — the value must never be mistaken for the subcommand, or every
    downstream gate (merge/create/comment) silently skips that segment: the
    separated ``-R o/r`` form let ``gh pr -R o/r merge N --admin`` bypass ALL
    fail-closed merge gates (found 2026-08-13 via the escalation-gate review).
    Glued (``-Ro/r``) and ``--repo=o/r`` forms are single ``-``-prefixed tokens
    and were already skipped.
    """
    if not argv or _basename(argv[0]) != "gh":
        return None
    _VALUE_FLAGS = {"-R", "--repo"}
    for i, t in enumerate(argv[1:], 1):
        if t == "pr":
            skip_next = False
            for u in argv[i + 1 :]:
                if skip_next:
                    skip_next = False
                    continue
                if u in _VALUE_FLAGS:
                    skip_next = True
                    continue
                if not u.startswith("-"):
                    return u
            return None
    return None


def commit_skips_hooks(argv: list[str]) -> bool:
    """Whether a ``git commit`` argv carries --no-verify / -n (bundled or not).

    Parses real argv tokens, so a quoted ``'--no-verify'`` counts and an
    attached message ``-minitial`` does NOT (that is ``-m initial``).
    """
    if git_subcommand(argv) != "commit":
        return False
    # Advance past git global options (+ their values) to the "commit" token.
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in _GIT_OPTS_WITH_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break  # argv[i] == "commit"
    i += 1  # move past "commit"
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break  # everything after -- is a pathspec, not a flag
        if tok == "--no-verify":
            return True
        if tok.startswith("--"):
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            consumes_next = False
            j = 1
            while j < len(tok):
                ch = tok[j]
                if ch == "n":
                    return True
                if ch in _COMMIT_ARG_FLAGS:
                    # a message/file flag: its value is the rest of this token,
                    # or — if it is the last char — the NEXT token (skip it, so a
                    # message beginning with "-n…" is not re-scanned as a flag).
                    consumes_next = j == len(tok) - 1
                    break
                j += 1
            i += 2 if consumes_next else 1
            continue
        break  # a bare positional (pathspec) — no more flags
    return False


def executes(command: str, exe: str, subcommand: str | None = None) -> list[Segment]:
    """All segments running ``exe`` (optionally with a given git subcommand)."""
    hits = []
    for seg in analyze(command):
        if seg.exe != exe:
            continue
        if subcommand is not None and git_subcommand(seg.argv) != subcommand:
            continue
        hits.append(seg)
    return hits
