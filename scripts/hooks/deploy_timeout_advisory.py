#!/usr/bin/env python3
"""PreToolUse(Bash) advisory: a deploy launched with update.sh must be DETACHED.

ADVISORY ONLY. It never blocks and never asks: it adds one note to the session's
context and always exits 0, and it states no ``permissionDecision`` (an
``allow`` would skip the permission prompt for the very command it warns
about). A dispatched session sees the note exactly as a foreground one does.

WHY IT EXISTS. ``scripts/update.sh`` cannot be held by a Bash call. The tool's
hard ceiling is 600000ms and a bare update.sh ran 1022s (MEASURED 2026-09-16), so
a foreground run is killed; ``run_in_background`` has no ceiling but is bound to
the session's lifetime, and deploys launched that way were killed mid-run on
2026-07-22 and 2026-09-16. The advisory prescribes ``systemd-run --user``.

HISTORY. PR #1221 shipped the first version of this hook, prescribing
``run_in_background``. An incident that same day refuted that remedy, and the hook
taught it for 56 days until a third deploy was killed (fixed in #2101). That
version also returned early on ``run_in_background``, so it was silent on exactly
the input that killed deploys. A remedy is only as good as the case it was
actually executed against.

WHEN IT FIRES (#2088). The bash version matched the substring ``update.sh``
anywhere in the command, so every read of the file drew the full advisory
(about 5.9 KB). This version parses the command with ``shell_parse`` and is an
ALLOWLIST FOR SILENCE: it stays quiet only when every part of the command is
shown not to run the script. Each rule below says when it is SILENT; anything
it does not cover fires.

- No ``update.sh`` in the text: silent, before the parser is even imported (the
  hot path, every Bash call).
- A blind parse, a parse error, a missing parser, or a crash: fires.
- An active process substitution (``<(…)``, ``>(…)``): fires; quoted literal
  spellings remain data. The parser does not see inside an active one.
- A segment that can run the script WITHOUT naming it fires: a shell reading
  stdin (no script operand, or ``-s``), a shell given a ``$`` operand, a
  program held in a variable (``$S``), ``eval``, and ``source``/``.`` of the
  script or of a variable.
- A heredoc first goes through ``shell_parse.excise_data_heredoc``, which
  proves the commit and PR-body forms are data. Otherwise its body is judged
  by its receiver: a shell or ``ssh`` reads it as commands; ``python``/``node``
  stay silent only with no process-spawn call in the body or in their own
  ``-c``; ``cat``/``tee`` and the other readers treat it as data; ANY other
  receiver (``systemd-run``, ``incus exec``, ``su``, ``perl``…) fires. A body
  that never closes fires.
- A command with a pipe, an input redirect or a here-string feeds one program
  to another, and a variable can carry the script's path into a program that
  never names it (``V=scripts/update.sh; perl -e 'system("$V")'``). In
  either case EVERY program in it must be a known reader.
- Otherwise a segment that names the script stays silent only when it is a
  known reader: a closed list of programs that read, list, copy or describe
  (``grep``, ``sed``, ``cat``, ``cp``…), ``git`` with a read subcommand and no
  global ``-c``, a shell given ``-n`` before its script, a shell's ``-c``
  wrapper (its inner script is judged as its own segments), or ``python``,
  ``node`` or ``awk`` whose program text is INLINE and has no process-spawn
  call (a module, a script file or ``awk -f`` cannot be read, so it fires).
  An option that runs the script (``--upload-pack``, ``--compress-program``,
  ``--rcfile``, ``rg --pre``…, abbreviated or not) fires. ``perl`` and
  ``ruby`` always fire: their spawn spellings are too many to list.
- An unrecognised program fires, including one the parser got wrong.
  Silencing unrecognised programs was tried and measured: the parser reads
  ``X=$(id -u) systemd-run …`` as a program named ``-u)`` without reporting a
  blind spot, so a real launch went quiet.

MEASURED 2026-10-13 over 30 days of one install's transcripts (153 files):
881 Bash commands mention the script and about 25 of them launch it. The old
hook fired on all 881; this one fires on 197 and is silent on 684, and no
silent one is a launch (each launch-shaped silent command was read by hand; the
five found are quoted prose). It also fires on every one of 91 constructed
launch forms in the tests, 59 of which earlier drafts of this hook missed
(found by internal review passes, a cross-model one, the PR's first review
round and a class audit after it). That is a corpus and a matrix, not a proof:
a form nobody wrote down can still be silent.

A DETACHED launch still fires, on purpose: that is the moment the advice
applies, and recognising detachment would need systemd-run's option grammar, an
open set (the previous rewrite stalled on exactly that, #2088). Known misses,
stated rather than chased: ``sed 'e …'``, a script copied and then run under
another name (a git hook included), a script launched from inside another
program or script (``make``, a test, ``bash other.sh``) that the command does
not name, and a program carried in an environment variable or config rather
than in argv (``GIT_PAGER=``, ``BASH_ENV=``, ``EDITOR=``, ``git --config-env``,
a git or gh alias), and a command substitution inside an arithmetic subscript
that a reader evaluates (``[[ 1 -eq 'a[$(…)]' ]]``, ``declare -i``).
``bootstrap.sh`` and ``host-setup.sh`` are deliberately not covered: both need
a channel a detached service does not have.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TARGET = "update.sh"
# shell_parse, imported only once a command names the script (see main).
_PARSER = None

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# Shell options that take the next word as a value (so it is not the script).
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
# Options whose VALUE is a program or startup file the program runs, given as
# the next word (`git fetch --upload-pack <path>`, `sort --compress-program
# <prog>`, `bash --rcfile <file> -i`). The attached spelling (`--opt=<path>`,
# `-O<path>`) is caught for every program by _option_runs_target.
# An option's own word (`--opt=…`, `-O…`), not prose that happens to start with
# a dash (`'- Updates no longer …'`).
_OPTION_WORD = re.compile(r"-{1,2}[A-Za-z][^\s]*\Z")
_EXEC_VALUE_OPTIONS = {
    "git": frozenset(
        {"--upload-pack", "--receive-pack", "--exec", "-u", "-O", "--open-files-in-pager"}
    ),
    "sort": frozenset({"--compress-program"}),
    "rg": frozenset({"--pre", "--hostname-bin"}),
    **dict.fromkeys(("bash", "sh", "zsh", "dash", "ksh"), frozenset({"--rcfile", "--init-file"})),
}
# Interpreters whose program text is checked for a process-spawn call. perl and
# ruby are not here: their spawn spellings (backticks, %x, open with a pipe) are
# too many to list, so a perl or ruby segment that names the script fires.
_INTERPRETERS = frozenset({"python", "python3", "node"})
_SOURCERS = frozenset({"source", "."})
# Programs that read, list, copy, compare or describe their operands and never
# execute them. CLOSED: a program not here that names the script fires, and in
# a command with a pipe or an input redirect EVERY program must be here.
# fmt: off
_READ_VERBS = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "sed",
    "wc", "diff", "cmp", "ls", "stat", "file", "jq", "echo", "printf", "sha256sum",
    "md5sum", "realpath", "readlink", "basename", "dirname", "cp", "mv", "tee",
    "touch", "chmod", "rm", "shellcheck", "pgrep", "pkill", "test", "[", "[[",
    "for", "case", "gh", "cd", "pwd", "true", ":", "sort", "uniq", "cut", "tr",
    "nl", "column", "tac", "paste", "comm", "fold", "sleep", "date", "mkdir",
    "ps", "base64", "ruff", "export", "read", "declare", "local", "unset", "wait",
    "exit", "return", "shift", "break", "continue",
    # Shell keywords the parser can leave standing as a segment's program.
    "do", "done", "then", "else", "elif", "fi", "esac", "{", "}",
})
# fmt: on
# git subcommands that only read or record; anything else (rebase -x, bisect
# run, submodule foreach, an alias) can execute.
# fmt: off
_GIT_READ_SUBCOMMANDS = frozenset({
    "log", "show", "diff", "blame", "grep", "status", "ls-files", "add", "commit",
    "fetch", "rev-parse", "merge-base", "rev-list", "ls-remote", "ls-tree",
    "cat-file", "branch", "describe", "for-each-ref", "shortlog", "reflog",
    "name-rev", "remote",
})
# fmt: on
# A call that can start a process from interpreter code. Broad on purpose: a
# false match only costs a note.
_SPAWN = re.compile(r"subprocess|\bsystem\b|popen|\bexec[a-z]*\b|spawn|child_process|\bpty\b")
# awk runs a program through system() or a pipe in a print/getline.
_AWK_SPAWN = re.compile(r"\bsystem\b|(?<!\|)\|(?!\|)")
# A heredoc opener. The delimiter is any word bash accepts, so a `-` or `.` in
# it (`<<'END-NOTES'`) cannot end the body early and expose the lines after it.
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)\\?([^\s'\"<>;|&()]+)\1")
_PROCESS_SUBSTITUTION = re.compile(r"[<>]\(")
# Backslash pairs preserve expansion; a lone escape suppresses it. Quotes in
# an unquoted heredoc BODY are data and do not suppress shell expansion.
_HEREDOC_EXPANSION = re.compile(r"(?<!\\)(?:\\\\)*(?:\$\(|`)")
_ASSIGNMENTS_ONLY = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\+?=\S*\s*)+")
_STDIN_PATH = re.compile(r"/dev/(?:stdin|fd/)|/proc/[^/]+/fd/")
# Input that one program hands another: a pipe, an input redirect, a
# here-string. With any of these, the program on the receiving end can run what
# it is given without naming it, so only known readers stay silent.
_FEEDS_INPUT = re.compile(r"(?<!\|)\|(?!\|)|(?<!<)<(?![<(])|<<<")
# A variable can carry the script's path into a program that never names it
# (`V=scripts/update.sh; perl -e 'system("$V")'`, a `for` loop over it), so a
# command that reads one is judged the same way: only known readers stay silent.
_VARIABLE = re.compile(r"\$\{?[A-Za-z_0-9@*]")
ADVISORY = r"""ADVISORY: this mentions scripts/update.sh. If you are LAUNCHING a deploy, it must be DETACHED from this session. Neither way of holding it in a Bash call works.

FOREGROUND is impossible: the Bash tool's HARD CEILING is 600000ms (10 min), a larger 'timeout' value is not honoured, and an inner 'timeout N' does not extend it. MEASURED 2026-09-16: a bare update.sh ran 1022s. The SIGTERM at the ceiling is also not a silent truncation -- update.sh traps INT/TERM, and once the rollback trap is armed (just before the merge) an interrupt ROLLS BACK, unwinding a deploy that was going fine. Which phase a ten-minute kill lands in depends on the run, so treat it as "something will be undone", not as a clean stop.

run_in_background is NOT the fix, though this hook used to say so. It has no 120s ceiling (MEASURED: a 400s background task completed clean, exit 0), but it is bound to this SESSION's lifetime, and deploys launched that way were killed mid-run on 2026-07-22 (server left DOWN during bootstrap) and 2026-09-16.

Hand it to systemd instead, wrapping THE COMMAND YOU ACTUALLY RAN (keep your own flags, e.g. --post-merge):

  XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
  DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
  systemd-run --user --collect --unit genesis-deploy-manual \
    --working-directory=$HOME/genesis \
    --setenv=PATH="$PATH" --setenv=SSH_AUTH_SOCK --setenv=GENESIS_SYNC_PRIVATE_PATTERNS \
    /bin/bash -c 'mkdir -p ~/tmp; exec ./scripts/update.sh > ~/tmp/deploy-$(date +%Y%m%d-%H%M).log 2>&1'

  substituting the checkout root you actually ran from for $HOME/genesis if it differs -- install.sh supports noncanonical locations and update.sh derives its root from its own path, so the canonical path is an example, not a constant.

Two preconditions, both cheap to check BEFORE detaching:

  * LINGER: a --user unit dies with the user manager when your last login session ends unless linger is enabled. 'loginctl show-user $(id -u) -p Linger' must print 'Linger=yes'; if not, run 'loginctl enable-linger $(id -u)' first (one-time, needs sudo).
  * NONINTERACTIVE SUDO: update.sh runs bootstrap.sh on every run that applies a change (its "Run bootstrap" step), and bootstrap calls sudo. If sudo prompts anywhere on this install the detached deploy aborts partway through. Require NOPASSWD-style sudo: with per-terminal timestamps, refreshing the credential in YOUR terminal does not authorize the no-TTY sudo calls inside the transient unit, and a warm cache expires mid-deploy regardless. Check it in the same no-TTY service context the deploy will get, with the bus variables above set: 'systemd-run --user --wait --pipe --quiet --collect -- sudo -n true' must exit 0 (not '--scope', which systemd refuses to combine with --wait). If it fails, run the deploy in a terminal instead.

Each part is load-bearing, and the bus variables come FIRST for a reason. A CC session often has no XDG_RUNTIME_DIR, and without it 'systemd-run --user' cannot reach the user manager at all: MEASURED, a scrubbed environment gives "Failed to connect to bus: No medium found" and nothing starts. --setenv configures the prospective UNIT and cannot help the CLIENT connect, so it is no substitute. update.sh seeds exactly these two variables for the same reason.

The default transient SERVICE mode is what leaves the session (its parent is the user manager); --unit only gives it a fixed name for the verify step. 'systemd-run --user --scope' does NOT detach (a scope child keeps the caller's session id), and neither does setsid (it starts a new session but does not stop the caller waiting on it). --working-directory is required because systemd-run does not inherit this cwd. --setenv=PATH is required because a --user unit otherwise runs under systemd's default PATH, which omits ~/.local/bin. A transient service gets the user manager's environment, NOT your shell's, so anything else the deploy relies on needs its own --setenv -- SSH_AUTH_SOCK (a private SSH remote will not fetch without it) and GENESIS_SYNC_PRIVATE_PATTERNS (bootstrap branches on it) are forwarded above; naming an unset variable is harmless.

DO NOT apply this to bootstrap.sh or host-setup.sh, which is why this hook no longer fires on them at all. Both need a channel a detached service does not have. bootstrap.sh runs under set -euo pipefail and calls bare sudo (the journald and sshd drop-in steps' 'sudo mkdir'), so on any install where sudo prompts it aborts partway through configuring the machine -- and "sudo is passwordless here" is a fact about one box, not about the recipe. host-setup.sh is worse: it is interactive, runs on the bare host VM, and its "Delete and recreate? [Y/n]" prompt treats EOF as the default Y, so detaching it stops and renames the existing container. update.sh is the one script in this family with no sudo calls of its own — it does invoke bootstrap.sh (its "Run bootstrap" step), which is why the sudo -n precondition above exists — and that is still the closest to detachable in the family, which is why it is the only one this advisory covers. Run the other two in a real terminal.

Then VERIFY it took: with the same two bus variables set, 'systemctl --user is-active genesis-deploy-manual' must print 'active' (without them a scrubbed session gets a bus error, which is not evidence the launch failed). A failed launch is otherwise indistinguishable from a good one -- with a relative path and no --working-directory, bash exits instantly and --collect reaps the unit, leaving no trace. Check promptly (it also reads 'inactive' once the deploy has FINISHED), then poll the log. genesis-server going 'inactive' mid-run is EXPECTED.

This hook does not check whether you already detached, so if you did, ignore it. Rationale, measurements and incident history: the validating-merges skill ("DEPLOYS ARE THE EXCEPTION") and the build-session skill's Timeout Policy."""


def _names_target(segment) -> bool:
    return any(_TARGET in token for token in (segment.exe, *segment.argv))


def _shell_operand(argv: list[str], *, parser=None, _tail=None) -> tuple[str | None, bool, bool]:
    """(first operand, reads stdin, syntax check only) for a shell's argv."""
    exe = os.path.basename(argv[0])
    parser = parser if parser is not None else _PARSER
    allowed = parser._C_BUNDLE_OPTIONS[exe]
    takes_value = parser._C_VALUE_TAKING.get(exe, parser._C_VALUE_TAKING_DEFAULT)
    i = 1
    noexec = sticky = False
    while i < len(argv):
        arg = argv[i]
        values = (
            1 if arg in _SHELL_VALUE_OPTIONS else
            sum(c in takes_value for c in arg[1:]) if not arg.startswith("--") else 0
        )
        if arg.startswith(("-", "+")) and arg not in ("-", "+", "--"):
            # Fold one option bundle through the existing state machine. The
            # local walk still accepts pre-script long options such as --norc;
            # _first_operand's post--c long-option refusal is not their grammar.
            _, _, noexec, sticky = parser._first_operand(
                argv[i : i + 1 + values], 0, allowed, takes_value, noexec, sticky
            )
        if arg in _SHELL_VALUE_OPTIONS:
            i += 2
            continue
        if arg == "--":
            i += 1
            break
        if arg.startswith(("-", "+")) and arg not in ("-", "+"):
            if not arg.startswith("--") and "s" in arg[1:]:
                return None, True, False  # -s: commands come from stdin
            i += 1 + values
            continue
        break
    operand = argv[i] if i < len(argv) and argv[i] != "-" else None
    if _tail is not None and operand is not None:
        _tail.extend(argv[i + 1:])
    return operand, operand is None, noexec or sticky


def _is_consumer(segment, *, fed_heredoc: bool) -> bool:
    """Can this segment run the script without naming it?"""
    exe = segment.exe
    if not exe or fed_heredoc:
        return False  # a heredoc's receiver is judged by its body instead
    if exe.startswith("$") or exe == "eval":
        return True
    if exe in _SOURCERS:
        return any(_TARGET in a or "$" in a for a in segment.argv[1:])
    if exe in _SHELLS:
        if _PARSER._nested_script(segment.argv, exe):
            return False
        operand, reads_stdin, _ = _shell_operand(segment.argv)
        return reads_stdin or "$" in (operand or "")
    return False


def _option_runs_target(segment) -> bool:
    """Does an option of this program name the script as something to run?

    The script in an option's own word (``--upload-pack=…``, ``-O…``,
    ``--compress-program=…``) is never a file being read, so any program fires
    on it. A separate value counts only after an option the program is known
    to execute (:data:`_EXEC_VALUE_OPTIONS`), because ``grep -n update.sh`` is
    an ordinary read.
    """
    argv = segment.argv
    if any(_OPTION_WORD.match(a) and _TARGET in a for a in argv[1:]):
        return True
    runs_value = _EXEC_VALUE_OPTIONS.get(segment.exe, frozenset())
    return any(
        _runs_value(a, runs_value) and _TARGET in b
        for a, b in zip(argv[1:], argv[2:], strict=False)
    )


def _runs_value(word: str, options: frozenset) -> bool:
    """Is *word* one of *options*, or a GNU abbreviation of a long one?

    ``sort --compress-prog X`` and ``git fetch --upload-p X`` run X exactly as
    the full spelling does (MEASURED by the class audit with a stub script).
    Matching a prefix the program would reject as ambiguous only over-fires.
    """
    if word in options:
        return True
    if not word.startswith("--") or len(word) < 4:
        return False
    return any(o.startswith("--") and o.startswith(word) for o in options)


def _git_reads(segment) -> bool:
    if any(a.startswith("ext::") for a in segment.argv[1:]):
        return False  # an explicit remote-helper program, not hidden config
    subcommand = _PARSER.git_subcommand(segment.argv)
    position = segment.argv.index(subcommand) if subcommand in segment.argv else len(segment.argv)
    # A global `-c` can define an alias that runs a program
    # (`git -c alias.x='!cmd' x`); after the subcommand, `-c` is that
    # subcommand's own option (`git log -c` is a combined diff).
    global_config = any(a.startswith("-c") for a in segment.argv[1:position])
    reads_config = subcommand == "config" and any(
        a in ("--get", "--get-all", "--list", "-l") for a in segment.argv
    )
    reads = subcommand in _GIT_READ_SUBCOMMANDS or reads_config
    return not global_config and reads


def _is_reader(segment) -> bool:
    """Is this segment known NOT to execute anything it is given?"""
    exe, argv = segment.exe, segment.argv
    if not exe:
        # An assignment alone runs nothing (its substitution is its own
        # segment); a bare wrapper (`sudo -s`) also leaves no program.
        return bool(_ASSIGNMENTS_ONLY.fullmatch(segment.raw.strip()))
    if _option_runs_target(segment):
        return False
    if exe in _SOURCERS:
        return not any(
            _TARGET in a or "$" in a or a == "-" or _STDIN_PATH.match(a) for a in argv[1:]
        )
    if exe in _SHELLS:
        # A `-c` wrapper's inner script is judged as its own segments; `-n`
        # before the script only checks syntax.
        return bool(_PARSER._nested_script(argv, exe)) or _shell_operand(argv)[2]
    if exe in _INTERPRETERS:
        # Given the script's name, the program must be inline (`-c`/`-e`) so
        # it can be checked: a module or a file (`python -m genesis.hostmetrics
        # run -- ./scripts/update.sh`, `node runner.js ./scripts/update.sh`) can
        # run whatever it is handed. MEASURED: both launched a stub.
        if _names_target(segment) and not any(a in ("-c", "-e") for a in argv):
            return False
        # Otherwise its program must be in argv (`-c`/`-e` or a script file)
        # to be checked; one read from stdin could be anything.
        has_program = any(a in ("-c", "-e") for a in argv) or any(
            not a.startswith("-") for a in argv[1:]
        )
        return has_program and not _SPAWN.search(" ".join(argv[1:]))
    if exe == "awk":
        # A program file (`-f`) is not text this hook can read.
        program_file = any(a.startswith(("-f", "--file")) for a in argv[1:])
        if _names_target(segment) and program_file:
            return False
        return not _AWK_SPAWN.search(" ".join(argv[1:]))
    if exe == "git":
        return _git_reads(segment)
    if exe == "rg":
        # --pre and --hostname-bin run a program (attached or abbreviated forms
        # included); the separate-value form is caught by _option_runs_target.
        return not any(_runs_value(a.split("=", 1)[0], _EXEC_VALUE_OPTIONS["rg"]) for a in argv)
    return exe in _READ_VERBS


def _after_comment(command: str, at: int) -> bool:
    """Is position *at* inside a ``#`` comment on its line (outside quotes)?"""
    start = command.rfind("\n", 0, at) + 1
    quote = None
    for i in range(start, at):
        c = command[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == start or command[i - 1] in " \t;&|("):
            return True
    return False


def _split_heredoc(command: str):
    """None without a heredoc; else (outer, body, before, closed, quoted).

    ``outer`` is the command with the first heredoc's body removed. The receiver
    is the last command in the text before the operator: the parser drops a
    ``<<`` redirect from a segment's raw text, so it cannot be found there.
    """
    match = _HEREDOC.search(command)
    if match is None or _after_comment(command, match.start()):
        # A `<<` inside a comment opens nothing; the parser reads the lines
        # after it as commands, which they are.
        return None
    newline = command.find("\n", match.end())
    if newline < 0:
        return None
    word = match.group(2)
    body: list[str] = []
    rest: list[str] = []
    closed = False
    for line in command[newline + 1 :].split("\n"):
        if not closed and line.strip() == word:
            closed = True
            continue
        (rest if closed else body).append(line)
    outer = command[:newline] + "\n" + "\n".join(rest)
    quoted = bool(match.group(1)) or "\\" in match.group(0)
    return outer, "\n".join(body), command[: match.start()], closed, quoted


def _depth0(segments) -> list:
    return [s for s in segments if s.depth == 0]


def _active_process_substitution(text: str) -> bool:
    """Active syntax in one parser segment, rather than quoted/escaped prose.

    The shared parser supplies nested command/substitution segments separately.
    This scanner only distinguishes their local quotes and escapes.
    """
    quote = None
    escaped = False
    for i, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "<>" and text[i + 1 : i + 2] == "(":
            return True
    return False


def _process_substitution_runs(command: str, segments) -> bool:
    """Inspect raw syntax plus command bodies supplied by the existing parser."""
    pending = [(command, 0)] + [
        (_PARSER._nested_script(s.argv, s.exe), 0) for s in segments if s.exe in _SHELLS
    ]
    while pending:
        text, depth = pending.pop()
        if _active_process_substitution(text):
            return True
        if depth >= 32 or len(pending) >= 128:
            return True  # cannot prove silence beyond this bounded traversal
        pending.extend((body, depth + 1) for body in _PARSER._substitutions(text))
    return False


def _runs_target(command: str) -> bool:
    """True unless the command is shown to only read, list or describe the script."""
    fed_position = None
    excised = _PARSER.excise_data_heredoc(command)
    if excised is not None:
        command = excised[0]
    else:
        split = _split_heredoc(command)
        if split is not None:
            outer, body, before, closed, quoted = split
            if not closed:
                return True  # an unclosed body could hide anything after it
            if not quoted and _HEREDOC_EXPANSION.search(body):
                return True  # expansion happens before even a data reader starts
            if not re.split(r"[;&|\n(]", before)[-1].strip():
                return True  # `<<EOF python3`: the receiver follows the operator
            before_segments, before_blind = _PARSER.analyze_checked(before)
            receivers = _depth0(before_segments)
            if before_blind is not None or not receivers:
                return True
            receiver = receivers[-1]
            fed_position = len(receivers) - 1
            if receiver.exe in _SHELLS or receiver.exe == "ssh":
                if _runs_target(body):
                    return True
            elif receiver.exe in _INTERPRETERS:
                # The code that spawns may be the body or the receiver's own
                # `-c` text running what the body hands it on stdin.
                if _SPAWN.search(body) or _SPAWN.search(" ".join(receiver.argv[1:])):
                    return True
            elif receiver.exe not in _READ_VERBS:
                return True  # systemd-run, incus, docker, su, perl, …: may run it
            command = outer
    segments, blind = _PARSER.analyze_checked(command)
    if blind is not None:
        return True
    if _PROCESS_SUBSTITUTION.search(command) and _process_substitution_runs(command, segments):
        return True  # active process-substitution bodies are not parsed
    strict = bool(_FEEDS_INPUT.search(command) or _VARIABLE.search(command))
    position = -1
    for segment in segments:
        if segment.depth == 0:
            position += 1
        fed = segment.depth == 0 and position == fed_position
        if _is_consumer(segment, fed_heredoc=fed):
            return True
        # A heredoc's receiver is excused from reading stdin (its body was
        # judged above), never from what its own argv names: `bash
        # scripts/update.sh <<EOF` still runs the script.
        judged = _names_target(segment) or (strict and not fed)
        if judged and not _is_reader(segment):
            return True
    return False


def _command(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def _emit() -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": ADVISORY,
        }
    }
    try:
        from hook_output import print_json_bounded

        print_json_bounded(payload, text_keys=("hookSpecificOutput.additionalContext",))
    except Exception:  # noqa: BLE001 - the note matters more than the bound
        import json

        print(json.dumps(payload))


def main() -> int:
    try:
        import json

        payload = json.loads(sys.stdin.read() or "null")
    except Exception:  # noqa: BLE001 - unreadable payload: nothing to judge
        return 0
    command = _command(payload)
    if _TARGET not in command:
        return 0  # the hot path: every Bash call, and nothing imported yet
    global _PARSER
    try:
        import shell_parse

        _PARSER = shell_parse
        fire = _runs_target(command)
    except Exception:  # noqa: BLE001 - no parser, or a crash: never silence
        fire = True
    if fire:
        _emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
