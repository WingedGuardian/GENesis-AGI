#!/usr/bin/env python3
"""Say, at every refusal, that the WHOLE Bash command was discarded.

A PreToolUse hook that exits 2 discards the **whole** Bash call, not the step it
objected to. So a command shaped::

    cat > config.py <<'EOF' … EOF && git commit -m 'x'

refused for the commit **also loses the write** — and the refusal message names
only the commit, so it reads as "the commit didn't happen", never "and the edit
you just made never happened".

WHY THIS LIVES AT THE REFUSAL POINTS, not in a hook of its own
--------------------------------------------------------------
A standalone hook that tried to predict which commands *would* be refused has to
restate every guard's block conditions, and drifts out of sync with them. The
guard about to refuse is the only thing that knows a block is happening, so the
note is emitted there. Nothing new runs on the allow path.

WHY IT NAMES NOTHING — measured twice, refuted twice
----------------------------------------------------
Two richer designs were tried and both failed on evidence rather than taste.

*Naming the FILES the command would have written* means mapping argv to EFFECT —
which of ``sed``'s spellings mean in-place, which operand is a program rather
than a path. That set has no closed boundary; it drew fourteen findings, each fix
shipping the next round's defect.

*Listing the discarded STEPS verbatim* looks safe — raw text, no classification —
and is not. Replayed against the real defect it renders ``1. cat  2. PORT=8080
3. EOF``: ``parse_segments`` splits on ``|`` as well as ``&&``/``;`` and retains
no separator, so a pipeline stage is indistinguishable from a step (measured on
real commands: median 4 "earlier steps", p90 17, max 158), and a plain-filename
redirect target is dropped from both text views BY DESIGN, so ``cat > config.py``
is just ``cat``. Recovering either means the parser work that produced the five
redirect-extraction findings.

So the note states the one thing true of EVERY block, needing no knowledge of any
tool and unable to go stale: the entire command was discarded. The reader has
their own command in front of them, which is what they must re-read anyway.

WHAT THIS IS WORTH, stated honestly
-----------------------------------
It fires on any multi-step command — 2,303 of 2,659 real commands (86.6%) — and
is load-bearing on the subset that actually carried a write, measured at 288 of
722 blocked multi-segment calls (~40%). On the rest it is true but not news.
Separating those needs the argv→effect mapping above, so the ubiquity is the
price of the note being unable to lie.

CONTRACT — this module is COSMETIC and must never change a verdict
------------------------------------------------------------------
* It only ever adds a message to a refusal that is already happening.
* Every entry point is fail-open: any parse failure, any unexpected exception,
  yields "no note" rather than raising. A guard's exit code is never touched.
* Callers MUST wrap the import itself in try/except. An unguarded import that
  failed would abort the guard's module load → exit 1 → which Claude Code treats
  as a NON-blocking error → the guarded command RUNS. A cosmetic helper must not
  be able to do that.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from shell_parse import split_segments  # noqa: E402
except Exception:  # noqa: BLE001 — a partially-synced hooks/ (this file present,
    # shell_parse absent) must degrade to SILENCE, not traceback into the refusal's
    # stderr. The sentinel is the house pattern (git_push_guard.py's push_allowlist);
    # callers below null-check it.
    split_segments = None  # type: ignore[assignment]

#: Phrased CONDITIONALLY ("if any earlier step …") on purpose. ``split_segments``
#: splits on ``|`` as well as ``&&``/``||``/``;``, so a pure PIPELINE counts as
#: multi-step — MEASURED at ≥9.5% of the commands this note fires on (219 of 2,303
#: real commands, a lower bound: it counts only those containing no ``&&``/``||``/
#: ``;``/newline anywhere at all). For those the first line is true and a flat
#: "an earlier step did not run" would not be, since a pipeline's stages are not
#: separable steps that were lost. Distinguishing them needs a separator the parser
#: does not retain, so the WORDING carries it instead of a richer predicate.
_NOTE = (
    "\nNOTE: the ENTIRE command was discarded, not just the step refused above.\n"
    "If any earlier step in it wrote a file, opened a heredoc, or changed\n"
    "directory, that did NOT happen either. Re-read the command before\n"
    "re-running it, and KEEP whatever guarded each step: a write behind a `&&`\n"
    "test was conditional, and re-running it alone can perform it in a state\n"
    "the original would have skipped."
)

#: The same fact in the tense an approval PROMPT needs. Nothing has been discarded
#: yet — the decision is still open — so this warns about what DECLINING costs. One
#: sentence, because it is appended to a dialog being read in the moment.
_PROMPT_NOTE = (
    "Declining also skips every OTHER step in this command — a file write, a "
    "heredoc, or a `cd` earlier in it will not run either."
)


def carried_more_than_the_refused_step(command: str) -> bool:
    """Whether the command holds more than one step, so a refusal lost collateral.

    Structural only: it counts the steps the shell would run and never inspects
    what any of them MEAN. No tool's option grammar can make it wrong, and a flag
    invented tomorrow cannot change its answer.

    ``split_segments`` is deliberately the whole predicate. It needs no cost bound
    of its own: it is a single-level, quote-aware split with NO recursion, linear
    in length (measured 0.049s on a 65 KB command), so there is no quadratic here
    for a budget to protect. ``analyze`` — which recurses, and which main bounds
    for exactly that reason — is not used, so this module is also outside the
    bare-``analyze`` allowlist by construction.

    ONE step means the refused step IS the whole call and there is nothing to
    report. Never raises — an unreadable command yields False, which is silence.
    """
    try:
        if split_segments is None or not command or not command.strip():
            return False
        return len(split_segments(command)) > 1
    except Exception:  # noqa: BLE001 — cosmetic: never break the guard that called us
        return False


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
    """Record the command this hook process is deciding about. Never raises.

    The try/except is NOT decoration, and the shipped body not being able to raise
    is not a reason to omit it. Three of this module's callers are not wrapped by
    ``run_guard``, so an exception escaping here exits 1 — which Claude Code reads
    as a NON-blocking error, running the very command the guard just refused.
    MEASURED with a deliberately poisoned build of this module: `git clean -fd`
    came back rc=1 from `git_discard_guard`, whose own docstring promises that a
    parser bug "can never become a silent ALLOW". Without this, the module
    docstring's "every entry point is fail-open" is true of three functions out of
    four, and the fourth is the one every caller invokes first.
    """
    global _COMMAND
    try:
        if isinstance(command, str) and command.strip():
            _COMMAND = command
    except Exception:  # noqa: BLE001 — cosmetic: never break the guard that called us
        pass


def warn(command: str | None = None) -> None:
    """Print the note to stderr, if there is one. Never raises, returns nothing.

    Call with no argument to use the command passed to :func:`remember`.
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
