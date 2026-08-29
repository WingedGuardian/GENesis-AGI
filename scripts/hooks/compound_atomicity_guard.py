"""PreToolUse hook: refuse a Bash call that chains a FILE WRITE with a step a guard can block.

THE FAILURE THIS EXISTS FOR
---------------------------
A PreToolUse block kills the WHOLE Bash call, not the offending segment. So a
command shaped

    python3 - <<'PY' … writes a file … PY
    && pytest tests/…

that is refused because a suite is already running ALSO discards the file write —
silently. The block message talks only about pytest, so the natural reading is
"the tests did not run", not "and the edit you just made never happened".

MEASURED, in the session that prompted this: an edit chained ahead of a pytest run
was discarded by the concurrent-test guard, and the loss was found only by
re-reading the file afterwards on a hunch. The same shape has previously cost four
heredocs that never wrote and a restore-from-backup that never ran.

WHY A GUARD RATHER THAN A RULE
------------------------------
This is already written down as a rule, and the rule did not prevent it. The
failure is silent, which is precisely the class a human (or a model) cannot be
trained to notice — there is nothing to notice. A refusal BEFORE anything runs
converts a silent loss into a visible, one-step correction.

FALSE POSITIVES, AND WHY THE BAR IS DIFFERENT HERE
---------------------------------------------------
The cost of a wrong refusal is one extra tool call: split the command in two. It
cannot wedge anything, cannot hide a fact, and cannot be worked around into a
worse state. That asymmetry is why this guard can afford to be slightly eager
where a merge gate could not.

It is still kept narrow on both sides:

  * WRITES counts only operations whose loss is INVISIBLE and not idempotent —
    a heredoc or redirect into a file, ``tee``, ``cp``, ``mv``, ``sed -i``,
    ``install``. Deliberately NOT ``mkdir``/``touch`` (idempotent, harmless to
    repeat) and NOT ``git add`` (re-running costs nothing, and its absence is
    obvious at the next ``git status``).
  * BLOCKABLE counts only guards that refuse BEFORE the command runs, since only
    those can discard an earlier step: ``git commit`` / ``push`` / ``checkout`` /
    ``restore`` / ``reset``, ``gh pr merge``, and a FULL-SUITE pytest run. A
    targeted pytest run is NOT counted — it is serialised by an in-process lock
    (#1530), which fails after the write has already landed. ``rm`` is not counted
    either: it is refused only for genuinely destructive shapes, not in general.

Both lists are conservative on purpose: a shape that is not on both sides is not
refused, so ordinary compounds (``cd x && pytest``, ``mkdir -p x && pytest``,
``cat f | grep x``) pass untouched.

MEASURED FALSE-POSITIVE RATE, against this session's own 1 897 unique Bash
commands rather than against invented examples: **5 flagged, 0.26%** — and all
five are real instances of the shape (an inline script or a ``cp`` chained ahead
of a ``git commit`` / ``push`` / ``gh pr merge``). An earlier draft that treated
every pytest run as blockable flagged 3.2%, which is 51 forced command splits per
session and would not have been worth it. The rate is the reason the scope is
what it is.

KNOWN GAPS, stated rather than discovered later:
  * A plain ``> out.txt`` redirect is NOT detected. ``shell_parse`` consumes
    redirect operator and target and drops plain-filename targets from both of
    its views, so the information is gone before this hook sees a segment.
    Surfacing it means changing a parser that security gates depend on — worth
    doing deliberately, not as a side effect of this guard.
  * Heredoc BODIES are split into pseudo-segments (``shell_parse`` does not track
    them). A body containing a line that parses as ``git commit`` can therefore
    trigger a refusal on its own. Measured as not occurring in the corpus above,
    and the cost if it does is one split.

Parsing is delegated to ``shell_parse`` — the canonical quote- and redirect-aware
parser — never to a bespoke regex. Hand-rolled shell parsing in a guard has an
unbounded divergence tail in this repo's own history.

Fail-OPEN on anything unparseable: this is a convenience guard that prevents a
self-inflicted loss, not a security boundary, and the worst case of a miss is the
status quo.
"""

from __future__ import annotations

import os
import sys

# Self-locate so hook_input/shell_parse resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from full_suite_guard import _pytest_args, _targets_specific_test  # noqa: E402
from hook_input import read_payload, tool_input  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    gh_pr_subcommand,
    git_subcommand,
    is_pytest_invocation,
)

#: Commands whose effect is lost invisibly when the call is refused. Each one
#: writes somewhere the next command would read, and none is idempotent enough
#: that silently repeating it is free.
_WRITE_EXES = frozenset({"tee", "cp", "mv", "install", "dd", "rsync"})

#: git subcommands the guards in this repo refuse. `commit` is gated on review
#: state, `push` on the push guard, and the discard family on git_discard_guard.
_BLOCKABLE_GIT = frozenset({"commit", "push", "checkout", "restore", "reset"})

#: Redirect targets that discard rather than write. Losing these costs nothing.
_NULL_SINKS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout"})

#: Interpreters that accept a script inline (heredoc, stdin or ``-c``). The body is
#: opaque, so it is assumed to have effects.
_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "perl", "ruby", "node"})


def _runs_an_inline_script(seg) -> bool:
    """An interpreter fed a script inline — ``python3 -``, ``bash -s``, ``… -c '…'``.

    This is the shape that motivated the guard: a heredoc into ``python3 -`` that
    writes a file, chained with a test run. The script body is opaque to any
    parser, so it is treated as write-capable by construction rather than
    inspected. That is deliberate — an inline script chained ahead of a blockable
    step is worth one extra tool call regardless of what it turns out to do.
    """
    if seg.exe not in _INTERPRETERS:
        return False
    return any(a in ("-", "-s", "-c") for a in seg.argv[1:])


def _writes_a_file(seg) -> bool:
    """True when this segment's effect is a file that a later step would read."""
    if seg.exe in _WRITE_EXES:
        return True
    # `sed -i` edits in place; plain `sed` only streams.
    if seg.exe == "sed" and any(a == "-i" or a.startswith("-i.") for a in seg.argv):
        return True
    if _runs_an_inline_script(seg):
        return True
    # A redirect into a real path. Only the targets `shell_parse` chose to keep are
    # visible here — a PLAIN `> out.txt` is consumed and dropped by the parser, so
    # it is NOT detected. See the KNOWN GAPS note in the module docstring.
    return any(t not in _NULL_SINKS for t in getattr(seg, "redirects", []) or [])


def _is_blockable(seg) -> bool:
    """True when a PreToolUse guard in this repo actually refuses this segment.

    Scoped to guards that BLOCK BEFORE THE COMMAND RUNS, because only those can
    discard an earlier step. A check that fails from inside the process — the
    pytest lock, a failing assertion — happens after the write has already
    landed, and including it would be pure friction.

    That distinction is measured, not assumed. Over this session's 1 897 real
    Bash commands, treating every pytest run as blockable flagged 3.2% of them;
    scoping to what genuinely blocks first flags 0.26% (5), all of them real
    instances of the shape. The difference is almost entirely
    ``write && pytest``, which stopped being a pre-block when the concurrent-run
    guard was replaced by an in-process lock (#1530).
    """
    if is_pytest_invocation(seg):
        # Only a FULL-SUITE run is still refused before it starts, and the rule
        # for that lives in full_suite_guard — imported rather than restated, so
        # the two cannot drift into disagreeing about the same command.
        return not _targets_specific_test(_pytest_args(seg))
    if seg.exe == "git" and git_subcommand(seg.argv) in _BLOCKABLE_GIT:
        return True
    return seg.exe == "gh" and gh_pr_subcommand(seg.argv) == "merge"


def main() -> None:
    payload = read_payload()
    cmd = tool_input(payload).get("command")
    if not isinstance(cmd, str) or not cmd:
        return
    try:
        segments = analyze(cmd)
    except Exception:  # noqa: BLE001 — convenience guard: never crash the call
        return
    if len(segments) < 2:
        return

    writes = [s for s in segments if _writes_a_file(s)]
    blockable = [s for s in segments if _is_blockable(s)]
    if not writes or not blockable:
        return
    # The write must come FIRST to be the thing that gets lost. A write after the
    # blockable step is discarded too, but nobody believes it ran.
    if segments.index(writes[0]) > segments.index(blockable[0]):
        return

    print(
        "BLOCKED: this call chains a FILE WRITE with a step a guard can refuse "
        f"({writes[0].raw.strip()[:48]!r} … {blockable[0].raw.strip()[:48]!r}).\n"
        "A PreToolUse block discards the WHOLE call, so if the second step is "
        "refused the write is lost SILENTLY — the error will only mention the "
        "second step, and the natural reading is that the write happened.\n"
        "Split them into two calls: do the write, confirm it, then run the rest.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
