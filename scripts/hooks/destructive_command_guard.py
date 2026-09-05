#!/usr/bin/env python3
"""PreToolUse hook (Bash): block rm with recursive+force on broad paths.

Catches rm with recursive+force flags targeting shallow paths that
could wipe important directories. A path must be at least 4 components
deep (e.g., /home/user/project/some_dir) to pass.

Blocks:  rm -rf /  |  rm -r -f ~  |  rm --recursive --force .  |
         rm -Rf ~/project  |  rm -rf -- /  |  rm -rf deep/path /
Allows:  rm -rf /home/user/project/.claude/worktrees/old-branch

Parsing is token-based (shlex): flags accumulate across tokens, `--`
ends flag parsing, and every operand is depth-checked individually —
the 2026-07-10 P1 triage empirically confirmed the old single-token
regex missed the `-r -f`, `--recursive --force`, `-Rf`, and `-- /`
spellings, and folded multiple operands into one pseudo-path.

Stdlib-only. Unparseable commands fall back to the legacy regex match
(fail-open beyond that — this guard must not block legitimate work).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import brace_expand, field, read_payload, run_guard  # noqa: E402

# Legacy single-token pattern — kept as the fallback when shlex cannot
# tokenize the command (unmatched quotes etc.).
_RM_RF_PATTERN = re.compile(
    r"\brm\s+"
    r"(?:-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)"
    r"\s+"
)

# Dangerous special targets — always block regardless of depth
_ALWAYS_BLOCK = {".", "..", "/", "~", "*"}

# Command separators that start a new simple command within one Bash
# string. Tokens matching these end an rm invocation's argument list.
_SEPARATORS = {"|", "||", "&&", ";", "&", "\n"}

# A redirection-shaped rm-operand token to SKIP: one that starts with a
# redirection operator — optional fd digits then `<`/`>` (`>`, `>log`,
# `2>/dev/null`, `2>&1`), or `&>` (`&>/dev/null`). Used with .match(), so it is
# anchored at the start and the WHOLE token is skipped. These are not rm targets;
# left in the operand list they are depth-checked as bogus shallow paths
# (`2>/dev/null` → depth 3 → spurious block).
#
# DESIGN (user decision 2026-07-24, "simple + catastrophe-safe" — see the code
# review saga in this file's git history): shlex erases the quoted/unquoted
# distinction (a file literally named `>` and a real redirect both tokenize to
# `>`), so redirect-shaped tokens are treated UNIFORMLY as redirects and skipped.
# The tradeoff is that a bizarrely-named file starting with `<`/`>` (e.g.
# `rm -rf ">etc"`) is allowed — deliberately accepted, because:
#   SAFETY PROOF — a skipped token always starts with `<`, `>`, or `&>` (or fd
#   digits then `<`/`>`). The always-block targets (`. .. / ~ *`) and every real
#   filesystem path never start that way, so skipping can NEVER drop a genuinely
#   dangerous target. And the guard never skips a token *following* a redirect,
#   so `rm -rf ">" /etc` still depth-checks and blocks `/etc`. All quote/escape
#   handling is left to shlex — no second, divergent parser (three such
#   divergences were bypasses across three review rounds, 2026-07-24).
_REDIR_TOKEN = re.compile(r"\d*[<>]|&>")

# A separator that ends an rm invocation's argument list, spaced into a
# standalone token below so shlex splits it. A lone `&` is spaced (background /
# new command) EXCEPT when it is part of a redirection: preceded by `<`/`>`
# (`2>&1`, `>&2`) or followed by `>` (`&>`). (`&&` is matched as a unit first.)
_SEPARATOR_SPACING = re.compile(r"(\|\||&&|[|;]|(?<![<>])&(?!>))")

# A line continuation: a backslash-newline whose backslash is NOT itself escaped.
# `(?<!\\)` anchors the match at the START of a backslash run; `((?:\\\\)*)`
# consumes the escaped PAIRS (kept via the backreference); the trailing `\\\n` is
# the odd one out — the real continuation — and is dropped with its newline.
# An even-length run therefore matches nothing: its last backslash is a literal
# character and the newline after it stays a command separator. Getting this
# wrong is a guard bypass, not a cosmetic issue — see _rm_violations.
_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")

# Unquoted characters after which the shell starts a new word, and therefore
# after which a `#` opens a comment. This is the shell's metacharacter set plus
# whitespace — ENUMERATED against bash (each member checked by whether the text
# after `<char>#note` is executed or swallowed as comment), not inferred from the
# parser's shape. An earlier revision inferred it and was wrong in BOTH
# directions at once.
_COMMENT_OPENERS = frozenset(" \t\n|&;<>()")

# The character immediately before a `(` decides whether that parenthesis is
# WORD-FORM — part of the surrounding word, so its matching `)` leaves the word
# open and a `#` glued to it is NOT a comment — or COMMAND-FORM, where the `)`
# ends a command and a glued `#` does open a comment.
#
#   word-form   $( … )  $(( … ))   command/arithmetic substitution
#               <( … )  >( … )     process substitution
#               =( … )             array assignment (`a=(x)`, `a+=(x)`)
#               ?( *( @(           extglob patterns
#   command     ( … )              subshell
#               (( … ))            arithmetic command
#               x)                 case pattern, function definition `f()`
#               !( … )             see below — NOT an extglob pattern here
#
# `!(` is the member that cannot be settled from a static set, so the runtime
# decides it. It is an extglob pattern only when `shopt -s extglob` is set; this
# hook sees non-interactive `bash -c`, where extglob is OFF, and there `!(true)`
# is a `!` negation applied to a subshell — a COMMAND-form paren whose `)` really
# does let a following `#` open a comment. MEASURED both ways with
# `!(true)#note; touch MARKER`: extglob off, no marker (a comment opened);
# extglob on, marker present (no comment). The two answers are incompatible, so
# the default that this hook actually runs under wins, and `!` stays out.
#
# ENUMERATED against bash 5.2, one member at a time, with `<prefix>#note; touch
# MARKER`: the marker appears iff `#` did NOT open a comment, so the `; touch`
# ran. That spelling is used deliberately instead of a trailing continuation —
# with `<prefix>#note \`⏎`touch MARKER`, folding leaves `<prefix>#note touch
# MARKER`, whose first word is an assignment prefix for the `a=(x)` case, so
# `touch` runs as the command word and the marker appears in BOTH directions.
# That confound reported array assignment as command-form, which it is not.
#
# Deliberately no worked example here. Naming a construct beside a statement
# that a gate stopped working is a recipe, and this repository is public; the
# repo's own prose tripwire forbids the pairing but cannot see every construct
# name, so its silence is not permission. The shapes live as fixture rows in the
# guard's tests, where they are data rather than instruction.
#
# Getting a member wrong is a bypass in one direction or the other: calling a
# word-form `)` a boundary lets a glued `#` fake a comment and hide the next
# line, and calling a command-form `)` mid-word folds a continuation the shell
# does not fold, gluing the next command onto the comment text so no `rm` token
# survives. Both directions are covered by fixture rows in the guard's tests.
_WORD_PAREN_PREFIXES = frozenset("$<>=?*+@")


def _fold_continuations(cmd: str) -> str:
    """Delete the line continuations the shell deletes — and only those.

    A whole-string regex cannot decide this, because whether a backslash-newline
    is a continuation depends on the CONTEXT it sits in. A ``#`` comment ends at
    the newline and the shell does not continue it, so folding there deletes a
    real command separator and glues the following command into the comment
    text: no ``rm`` token survives, and because tokenizing then SUCCEEDS the
    legacy-regex fallback (which fires only when tokenizing FAILS) never runs.

    Both error directions are bypasses in this guard, which is why this tracks
    state instead of approximating either one. Failing to fold a genuine
    continuation splits a word the shell joins and can hide the recursive-force
    flags — the bypass recorded in ``_rm_violations``. Folding one the shell
    does not join hides the command itself. Contrast ``shell_parse``, where the
    consumers only ever over-read, so an approximation is safe there and is not
    safe here.

    Quotes keep their previous treatment deliberately: a ``#`` inside them opens
    nothing, and the continuation is still folded, because the shell keeps the
    sequence literally inside single quotes and folding it changes one operand's
    spelling, never its depth or its flags.

    Odd/even parity falls out with no counting: an escaped PAIR is consumed
    here, so a newline following an even-length run is seen fresh and stays a
    real separator. ``_CONTINUATION`` is retained as the parity reference this
    is checked against.
    """
    out: list[str] = []
    quote: str | None = None
    in_comment = False
    # The shell opens a comment at a WORD START only, so track that directly
    # rather than inferring it from the last emitted character. Inferring it was
    # wrong in both directions: the separators `;` `|` `&` reach the output as
    # themselves here (the spacing pass runs downstream, on this function's
    # result), so they were missed and the fold still ran inside a real comment;
    # and a `)` closing a word-form parenthesis (see _WORD_PAREN_PREFIXES) is
    # mid-word, so it was treated as a boundary and a real continuation was
    # refused — which splits a word the shell joins and can hide the
    # recursive-force flags.
    at_word_start = True
    # One entry per OPEN parenthesis, each classified from its OWN preceding
    # character: True = word-form (see _WORD_PAREN_PREFIXES), so its `)` closes an
    # expansion and the word continues; False = command-form, so its `)` is a
    # boundary and a `#` after it opens a comment.
    #
    # A stack rather than a depth counter, and each entry classified independently
    # rather than inheriting from the one below it. A counter cannot express either
    # property and was wrong twice over: nesting inherited, so a genuine subshell
    # inside `$( … )` was read as word-form and a real comment was missed; and the
    # count leaked whenever a partially-modelled context swallowed an opener or a
    # closer — a `)` inside a comment never decremented it, so every later `)` in
    # the command read as mid-word. Independent classification also keeps `$((`
    # right without a special case: the inner entry may be command-form, but the
    # outer word-form `)` is the one that decides where the word ends.
    paren_forms: list[bool] = []
    # The previous UNQUOTED, UNESCAPED character — what decides a `(`'s form.
    # A quoted or backslash-escaped character resets it to None: `\$(` is a
    # literal `$` followed by a subshell, not a command substitution.
    prev: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            if c == "\\" and i + 1 < n and cmd[i + 1] == "\n":
                i += 2  # fold inside quotes, as before
                continue
            out.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                out.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
                prev = None
            i += 1
            continue
        if not in_comment and c in ("'", '"'):
            quote = c
            out.append(c)
            at_word_start = False
            prev = None
            i += 1
            continue
        if c == "#" and not in_comment and at_word_start:
            in_comment = True
        if c == "\\" and i + 1 < n and not in_comment:
            if cmd[i + 1] == "\n":
                i += 2
                continue
            out.append(c)
            out.append(cmd[i + 1])
            at_word_start = False  # `a\ #x` is one word — the escaped space does not end it
            prev = None
            i += 2
            continue
        if not in_comment:
            if c == "(":
                paren_forms.append(prev in _WORD_PAREN_PREFIXES)
            elif c == ")" and paren_forms and paren_forms.pop():
                out.append(c)
                at_word_start = False  # closes an expansion, so still inside a word
                prev = c
                i += 1
                continue
        if c == "\n":
            in_comment = False  # a comment ends at the newline, never past it
        out.append(c)
        if not in_comment:
            at_word_start = c in _COMMENT_OPENERS
        prev = c
        i += 1
    return "".join(out)


def _check_target(target: str) -> str | None:
    """Reason string if *target* is too broad to rm recursively."""
    clean = target.strip("'\"")
    if clean in _ALWAYS_BLOCK:
        return f"rm -rf on '{clean}' is not allowed."
    expanded = os.path.normpath(os.path.expanduser(clean))
    parts = [p for p in expanded.split("/") if p]
    # A surviving '..' means the path traverses upward from a base the
    # hook cannot know (its cwd need not match the Bash invocation's).
    # normpath collapses interior '..' only against an absolute/~-anchored
    # path; a leading '..' on a RELATIVE path survives and is counted as
    # depth — so `rm -rf ../../../etc` reports depth 4 yet resolves to
    # /etc from filesystem root. Refuse rather than guess. (2026-07-10
    # review: this was a live bypass.)
    if ".." in parts:
        return f"rm -rf on '{clean}' traverses upward ('..') — refusing."
    if len(parts) < 4:
        return f"rm -rf on '{clean}' (depth {len(parts)}) is too broad."
    return None


def _rm_violations(cmd: str) -> list[str] | None:
    """Reasons to block, or None when the command cannot be tokenized."""
    # Line-continuations and bare newlines must be handled BEFORE shlex, which
    # drops a bare newline as whitespace (so a following command would fold into
    # the first rm's operands) and keeps an escaped newline INSIDE the token.
    #
    # A backslash-newline is folded to NOTHING, because that is what the shell
    # does: it is deleted, and the characters either side of it become one word.
    # An earlier version folded it to a space, which SPLITS a word the shell
    # joins. Measured: that let a continuation placed inside an option token turn
    # a recursive-force removal of a protected path into two tokens the guard
    # recognized as neither, and the command was allowed. The guard must read
    # the command the shell will run, not a nearby one. (Inside single quotes
    # the shell keeps the sequence literally; folding it there only changes the
    # spelling of one operand, never its depth or its flags.)
    #
    # ONLY AN ODD-LENGTH BACKSLASH RUN IS A CONTINUATION. In an even-length run
    # every backslash is escaped by its neighbour, so the last one is a literal
    # character and the newline after it is a REAL command separator. Folding it
    # anyway deleted that separator and glued the next command's first word onto
    # the previous token (`printf x` + `rm` -> `xrm`), so no `rm` token existed
    # and the guard allowed a destructive command the shell then ran. It returned
    # "no violations" rather than "unparseable", so main()'s legacy-regex fallback
    # — which only fires when tokenizing FAILED — never fired either.
    # `_CONTINUATION` therefore matches a backslash-newline only when the run
    # length is odd: the captured even pairs are kept, the final backslash and its
    # newline are dropped.
    #
    # These replacements delete a continuation or insert whitespace/`;`, never
    # quote or escape characters, so they cannot corrupt shlex's quote balance.
    # Redirections are NOT stripped here — they are recognized at the token
    # level after shlex (see _REDIR_TOKEN), so shlex stays the sole authority on
    # quoting/escaping.
    cmd = _fold_continuations(cmd).replace("\n", " ; ")
    # Space glued command separators (`x;y`, `a&&b`) into standalone tokens so
    # the operand loop stops at them; a redirection `&` is preserved.
    spaced = _SEPARATOR_SPACING.sub(r" \1 ", cmd)
    try:
        tokens = shlex.split(spaced)
    except ValueError:
        return None  # unparseable — caller falls back to the legacy regex

    violations: list[str] = []
    i = 0
    while i < len(tokens):
        # Resolve `rm` through a shell subshell opener GLUED to the command
        # (`(rm`). This guard scans every token, so SPACED control forms
        # (`( rm`, `then rm`, `{ rm`) already tokenize `rm` standalone; only the
        # glued opener hid it (`basename("(rm") == "(rm"`), a `(rm -rf /)` bypass.
        # Strip AT MOST ONE leading `(`: a single `(rm` is a real subshell rm
        # (block), but a doubled `((rm` is bash ARITHMETIC (`((…))` evaluates an
        # expression, runs no command) → leaving it as `(rm` (basename != rm)
        # correctly skips it. A glued trailing `)` on the target needs no handling
        # — it keeps a dangerous target shallow (`/)` → depth 1), which
        # _check_target blocks.
        core = tokens[i][1:] if tokens[i].startswith("(") else tokens[i]
        if os.path.basename(core) != "rm":
            i += 1
            continue

        # Parse this rm invocation until the next command separator.
        recursive = force = False
        operands: list[str] = []
        flags_done = False
        i += 1
        while i < len(tokens) and tokens[i] not in _SEPARATORS:
            arg = tokens[i]
            i += 1
            if not flags_done and arg == "--":
                flags_done = True
                continue
            if not flags_done and arg.startswith("--"):
                # GNU getopt_long accepts unambiguous prefix abbreviations,
                # so `--rec`/`--f` are valid spellings of --recursive/
                # --force. Match by prefix (len>=3 skips the bare '--').
                # Over-matching only over-blocks, which is safe here.
                # (2026-07-10 review: `rm --rec --f /` was a live bypass.)
                if len(arg) >= 3 and "--recursive".startswith(arg):
                    recursive = True
                elif len(arg) >= 3 and "--force".startswith(arg):
                    force = True
                continue  # other long flags carry no target
            if not flags_done and arg.startswith("-") and len(arg) > 1:
                if any(c in "rR" for c in arg[1:]):
                    recursive = True
                if "f" in arg[1:]:
                    force = True
                continue
            # A glued redirection token (`2>/dev/null`, `>log`, `2>&1`) is not
            # an rm operand — skip it rather than depth-check it. See _REDIR_TOKEN
            # for the safety argument (a match always starts with an operator
            # char no dangerous target contains, and the following token is
            # never skipped).
            if _REDIR_TOKEN.match(arg):
                continue
            operands.append(arg)

        if recursive and force:
            for operand in operands:
                # Bash brace-expands an unquoted operand before rm runs, so the
                # depth check must see each real target, not the opaque
                # `~/genesis/{data,logs}` token (which is depth-4 and passes).
                for expanded in brace_expand(operand):
                    reason = _check_target(expanded)
                    if reason:
                        violations.append(reason)
    return violations


def main() -> int:
    try:
        cmd = field(read_payload(), "command")
        if not cmd or "rm" not in cmd:
            return 0

        violations = _rm_violations(cmd)
        if violations is None:
            # Tokenizer failed — the legacy regex still catches the
            # common spelling; beyond that we fail open by design.
            if not _RM_RF_PATTERN.search(cmd):
                return 0
            violations = [
                "recursive+force rm inside an unparseable command — blocked conservatively."
            ]

        if violations:
            for reason in violations:
                print(f"BLOCKED: {reason}", file=sys.stderr)
            print(
                "Recursive+force rm targets must be at least 4 levels deep. "
                "If intentional, ask the user to confirm.",
                file=sys.stderr,
            )
            return 2

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    run_guard(main, "destructive_command_guard")
