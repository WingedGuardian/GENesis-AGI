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
* an approval comment ``# review-override`` that belongs to ONE command segment
  and must not authorize the next.

This module centralizes that parsing so ``git_push_guard``,
``review_enforcement_commit``, and the destructive/path guards agree. Stdlib
only; fail-open (a segment that won't tokenize degrades to a naive split rather
than raising) — a guard must never crash the tool.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

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


def split_segments(command: str) -> list[str]:
    """Split a command line into executed segments on shell operators.

    Quote-aware: an operator inside a quoted string does not split. Splits on
    ``&&``, ``||``, ``;``, ``|``, ``&`` and newlines. A ``#`` comment (opened
    outside quotes) runs to end-of-line and is retained in the segment text so
    override detection can see it.
    """
    segs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        two = command[i : i + 2]
        if two in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "|", "&", "\n"):
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


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
    for raw in split_segments(command):
        override = _has_trailing_override(raw)
        argv = _strip_wrappers(_argv(raw))
        exe = _basename(argv[0]) if argv else ""
        out.append(Segment(exe=exe, argv=argv, override=override, raw=raw))
        nested = []
        if exe in _NESTED:
            script = _nested_script(argv)
            if script:
                nested.append(script)
        nested.extend(_substitutions(raw))  # $(...) / `...` bodies also execute
        for script in nested:
            for inner in analyze(script):
                out.append(
                    Segment(
                        exe=inner.exe,
                        argv=inner.argv,
                        override=override or inner.override,
                        raw=inner.raw,
                        depth=inner.depth + 1,
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
    double quotes). Nested ``$()`` is balanced by paren depth. Best-effort:
    exotic forms (process substitution ``<(…)``, ANSI-C ``$'…'`` scripts, ``env
    -S`` string-splitting, shell aliases/functions) are NOT parsed — this guard
    is an approval/friction layer, not a sandbox, and fails toward over-matching
    on the common forms rather than pretending to cover every shell construct.
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
            depth, j = 1, i + 2
            start = j
            while j < n and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            subs.append(text[start : j - 1])
            i = j
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
