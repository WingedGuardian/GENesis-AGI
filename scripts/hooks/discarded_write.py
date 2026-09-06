#!/usr/bin/env python3
"""Say, at every refusal, that the WHOLE Bash command was discarded.

A PreToolUse hook that exits 2 discards the **whole** Bash call, not the step it
objected to. So a command shaped::

    cat > config.py <<'EOF' … EOF
    git commit -m 'x'

refused for the commit **also loses the write** — and the refusal message names
only the commit, so it reads as "the commit didn't happen", never "and the edit
you just made never happened". Measured on this box: of 722 multi-segment Bash
calls that a PreToolUse hook actually blocked, 288 carried a write nobody was
told about.

WHY THIS LIVES AT THE REFUSAL POINTS, not in a hook of its own
--------------------------------------------------------------
A standalone hook that tried to predict which commands *would* be refused has to
restate every guard's block conditions, and drifts out of sync with them. A first
attempt did exactly that and was measured wrong in BOTH directions — it refused
shapes no guard blocks, and missed shapes they do. The guard about to refuse is
the only thing that knows a block is happening, so the note is emitted there.

WHY IT NAMES NOTHING
--------------------
An earlier design worked out WHICH files the discarded command would have
written, and it did not converge. Naming a file means mapping argv to EFFECT —
which of ``sed``'s option spellings mean in-place, which operands are the program
rather than a path, which redirect forms are writes — and that set has no closed
boundary. Three review rounds produced fourteen findings, and the overwhelming
majority were one more spelling or one more operand class; each fix shipped the
next round's defect. Round two already deleted the file-list derivation for this
reason, after its own fix reported ``-e``'s VALUE as an edited file.

The deletion is not only about convergence. Assembling that list was quadratic in
the segment count, and MEASURED it turned this cosmetic helper into a fail-OPEN
in a security hook: ``rm -rf x && >q0000 >q0001 …`` at 63k chars sat inside every
input bound, cost 2.5s, and ``bash_safety_hook.sh`` pays that twice per block
against a 5-second registration — so the hook was SIGKILLed before reaching its
``exit 2``, and Claude Code reads a non-2 exit as non-blocking. The refused
``rm -rf`` ran. A helper that can do that has no business being clever.

So the note states the one thing that is true of EVERY block, needs no knowledge
of any tool, and cannot go stale: the entire command was discarded. The reader
has their own command in front of them and can see what was in it — which is what
they must re-read anyway before re-running it.

CONTRACT — this module is COSMETIC and must never change a verdict
------------------------------------------------------------------
* It only ever adds a message to a refusal that is already happening.
* Every entry point is fail-open: any parse failure, any unexpected exception,
  returns "no note" rather than raising. A guard's exit code is never touched.
* Callers MUST wrap the import itself in try/except. An unguarded import that
  failed would abort the guard's module load → exit 1 → which CC treats as a
  NON-blocking error → the guarded command RUNS. A cosmetic helper must not be
  able to do that.
"""

from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_parse import analyze, split_segments  # noqa: E402

#: A cheap FIRST PASS, not the bound. Both checks are O(n) scans, so an obviously
#: enormous command is refused without paying a parse at all.
_MAX_COMMAND_CHARS = 65_536
_MAX_SUBSTITUTIONS = 256

#: The ACTUAL bound: wall clock. The two constants above are PROXIES for cost, and
#: were MEASURED not to bound it. ``analyze`` recurses once per substitution NESTING
#: level and re-scans the remainder each time, so cost is length x nesting-depth —
#: the PRODUCT of two axes bounded independently above. Forty nested ``$(`` inside a
#: 65KB command sits inside BOTH proxies and cost 8s; 255 cost 82s. On
#: ``destructive_command_guard`` — which parsed nothing before this helper existed —
#: that exceeded the hook's 10s registration, and a hook SIGKILLed before its
#: ``exit 2`` is read by Claude Code as non-blocking: the refused ``rm -rf`` RAN.
#: origin/main refused the same command in 0.21s at every depth.
#:
#: Bounding the thing that actually matters, rather than a third proxy, is the whole
#: lesson: a budget cannot be defeated by an input shape nobody enumerated.
_BUDGET_SECONDS = 0.5


def _timed_out(_sig, _frame):
    raise TimeoutError("discarded-write predicate exceeded its budget")

_NOTE = (
    "\nNOTE: the ENTIRE command was discarded, not just the step refused above.\n"
    "Any earlier step in it — a file write, a heredoc, a `cd` — did NOT run.\n"
    "Re-read the command before re-running it, and KEEP whatever guarded each\n"
    "step: a write behind a `&&` test was conditional, and re-running it alone\n"
    "can perform it in a state the original would have skipped."
)


def carried_more_than_the_refused_step(command: str) -> bool:
    """Whether the command holds more than one step, so a refusal lost collateral.

    Structural only: this counts the steps the shell would run and never inspects
    what any of them MEAN. No tool's option grammar can make it wrong, and a flag
    invented tomorrow cannot change its answer.

    Counted at BOTH levels, because the guards block at both. ``split_segments``
    cuts only at the top, so steps living inside a nested script —
    ``bash -c 'cp a b && git commit …'``, or the same in a command substitution —
    read as one step and the note goes silent, while the guard recursively finds
    the inner command and blocks the whole call. MEASURED on that command:
    ``split_segments`` sees 1, ``analyze`` sees 3.

    But a bare ``len(analyze(...)) > 1`` is WRONG in the other direction, which a
    control caught before this shipped: ``analyze`` also yields the ``bash -c``
    WRAPPER, so ``bash -c 'git reset --hard'`` counts 2 and gets a note for
    collateral that does not exist. The wrapper is not a step the refusal
    discarded; it is the thing that was refused.

    The two NESTING KINDS therefore count differently, by ``Segment.via``:

    * More than one nested step, of either kind, is collateral — the original
      rule, unchanged.
    * A LONE ``"substitution"`` (``$(…)``, backticks — including one inside a
      redirect target) is collateral too, PROVIDED the top level carries a real
      host command: the substituted command runs IN ADDITION to the host, so
      ``git push --force "$(cp a b)"`` refused loses the ``cp`` — and a plain
      nested-count>1 said nothing. The host check is what keeps the bare
      ``$(git push)`` control silent: there the top-level "command" is the
      substitution expression itself (exe starts with ``$(`` or a backtick),
      the nested push IS the refused step, and nothing else was lost.
    * A lone ``"script"`` step (``bash -c 'git …'``) stays silent — the wrapper
      is not a step, so the script's one command is the whole call.

    ONE step means the refused step IS the whole call and there is nothing to
    report. Never raises — an unreadable command yields False, which is silence.
    """
    try:
        if not command or not command.strip():
            return False
        if len(command) > _MAX_COMMAND_CHARS:
            return False
        if command.count("$(") + command.count("`") > _MAX_SUBSTITUTIONS:
            return False
        # setitimer, not a proxy: see _BUDGET_SECONDS. The blanket handler below
        # catches the TimeoutError -> silence, this module's documented fail-open.
        # Off the main thread signal.signal raises ValueError, caught by the same
        # handler -> also silence. Both land on the safe side.
        prev = signal.signal(signal.SIGALRM, _timed_out)
        signal.setitimer(signal.ITIMER_REAL, _BUDGET_SECONDS)
        try:
            segments = analyze(command)
            nested = [s for s in segments if s.depth > 0]
            real_host = any(
                s.exe and not s.exe.startswith(("$(", "`"))
                for s in segments
                if s.depth == 0
            )
            return (
                len(split_segments(command)) > 1
                or len(nested) > 1
                or (real_host and any(s.via == "substitution" for s in nested))
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prev)
    except Exception:  # noqa: BLE001 — cosmetic: never break the guard that called us
        return False


#: The same fact, in the tense an approval prompt needs. Nothing has been discarded
#: yet — the decision is still open — so this warns about what DECLINING costs. It is
#: one sentence because it is appended to a permission dialog the user is reading in
#: the moment, not to a log they can scroll.
_PROMPT_NOTE = (
    "Declining also skips every OTHER step in this command — a file write, a "
    "heredoc, or a `cd` earlier in it will not run either."
)


def note(command: str) -> str | None:
    """The note to print beside a refusal, or None when there is nothing to say."""
    return _NOTE if carried_more_than_the_refused_step(command) else None


def prompt_note(command: str | None = None) -> str | None:
    """The note to append to an approval PROMPT, or None when there is nothing to say.

    A refusal has already thrown the command away; a prompt has not, so the two say
    different things and the tense matters. This one is decision-relevant: an operator
    reading "block the push?" may not realise that declining also drops the write two
    steps earlier in the same command.

    Call with no argument to use the command passed to :func:`remember`. Never raises.
    """
    try:
        cmd = command if command is not None else _COMMAND
        return _PROMPT_NOTE if cmd and carried_more_than_the_refused_step(cmd) else None
    except Exception:  # noqa: BLE001 — cosmetic
        return None


# ── remembered command ───────────────────────────────────────────────────────
# A guard reads its payload from stdin, which is CONSUMED by that read, so code
# further down (or a wrapper around main) cannot read the command again. Guards
# therefore hand it over once, where they already extract it. One hook process
# handles exactly one command, so a single module-level slot is sufficient and
# cannot be crossed with another command's.
_COMMAND: str | None = None


def remember(command: str | None) -> None:
    """Record the command this hook process is deciding about. Never raises."""
    global _COMMAND
    if isinstance(command, str) and command.strip():
        _COMMAND = command


def warn(command: str | None = None) -> None:
    """Print the note to stderr, if there is one. Never raises, returns nothing.

    Call with no argument to use the command passed to ``remember``.
    """
    try:
        cmd = command if command is not None else _COMMAND
        if not cmd:
            return
        text = note(cmd)
        if text:
            print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001 — cosmetic
        pass


def _main(argv: list[str]) -> int:
    """CLI for the shell hooks, so they call THIS implementation rather than
    re-deriving the check in bash.

    ``python3 discarded_write.py --command "$CMD"`` prints the note (if any) to
    stderr. Always exits 0: the caller's own exit code is the verdict, and this
    must not perturb it.
    """
    cmd = ""
    if "--command" in argv:
        idx = argv.index("--command")
        if idx + 1 < len(argv):
            cmd = argv[idx + 1]
    if not cmd and not sys.stdin.isatty():
        try:
            cmd = sys.stdin.read()
        except (OSError, ValueError):
            cmd = ""
    warn(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
