"""Name the file writes a refused command was carrying.

A PreToolUse hook that exits 2 discards the **whole** Bash call, not the step it
objected to. So a command shaped::

    python3 - <<'PY' … writes a file … PY
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

CONTRACT — this module is COSMETIC and must never change a verdict
------------------------------------------------------------------
* It only ever adds a message to a refusal that is already happening.
* Every entry point is fail-open: any parse failure, any unexpected exception,
  returns "no note" rather than raising. A guard's exit code is never touched.
* Callers MUST wrap the import itself in try/except. An unguarded import that
  failed would abort the guard's module load → exit 1 → which CC treats as a
  NON-blocking error → the guarded command RUNS. A cosmetic helper must not be
  able to fail-open a security guard.

Write detection is deliberately generous: a false positive costs one extra
sentence on a message that is already a refusal, while a false negative is the
silent data loss this exists to stop.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_parse import analyze, write_targets  # noqa: E402

#: Executables whose whole purpose is to put bytes somewhere.
_WRITE_EXES = frozenset(
    {
        "tee",
        "cp",
        "mv",
        "install",
        "dd",
        "rsync",
        "touch",
        "mkdir",
        "ln",
        "truncate",
        "patch",
    }
)

#: Interpreters that write when handed a script inline rather than a file path.
_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "perl", "ruby", "node"})

#: Flags that mean "the script is inline / on stdin" for the interpreters above.
_INLINE_FLAGS = frozenset({"-c", "-e", "-"})

_MAX_NAMED = 6  # keep the note short enough to read at the end of a refusal
_MAX_PHRASE = 160  # and keep any ONE entry from crowding out the others

# Bounds on how much work this is willing to do before giving up and saying
# nothing. `shell_parse.analyze` recurses once per level of nested `$(…)` and
# re-scans the remainder each time, so its cost is superlinear: MEASURED at 0.07s
# / 0.68s / 2.9s for 100 / 400 / 800 nested substitutions. That cost is paid on a
# BLOCK path, and a hook killed at its configured timeout has not reached its
# `exit 2` — which CC reads as a non-blocking error, i.e. the refused command
# runs. A cosmetic note must not be able to buy that, so it stops early instead.
#
# Sized from the real corpus (13,573 commands on one box): longest command 14,682
# chars, most substitutions in any one command 150. These sit ~4x and ~1.7x above
# those, so no observed real command is affected.
_MAX_COMMAND_CHARS = 65_536
_MAX_SUBSTITUTIONS = 256


def _inline_script(seg) -> bool:
    """Whether this segment runs an interpreter over INLINE code.

    ``python3 -`` (stdin, typically a heredoc), ``python3 -c '…'``, ``node -e '…'``.
    A path argument (``python3 script.py``) is NOT inline — the file already exists
    and re-running it costs nothing, so it is not a discarded write.
    """
    if seg.exe not in _INTERPRETERS:
        return False
    return any(a in _INLINE_FLAGS for a in seg.argv[1:])


def _short_cluster(arg: str) -> str | None:
    """The clustered short-option letters of ``arg``, or None if it is not a cluster.

    ``-Ei`` is ``-E -i``; a guard that only recognises a STANDALONE ``-i`` misses it.
    Modelling clustering is a documented requirement for argv guards in this repo,
    and skipping it is how this predicate missed half its own subject.
    """
    if len(arg) < 2 or not arg.startswith("-") or arg.startswith("--"):
        return None
    return arg[1:]


def _in_place_edit(seg) -> bool:
    """``sed``/``perl`` editing files IN PLACE, in every spelling those tools accept.

    GNU sed documents ``-i[SUFFIX], --in-place[=SUFFIX]``, and the suffix attaches
    with no separator — so ``-i``, ``-i.bak``, ``-ibak``, the cluster ``-Ei``, the
    long ``--in-place`` and ``--in-place=.bak`` are all in-place, as is perl's
    clustered ``-pi``. An earlier version recognised only ``-i`` and ``-i.``, so an
    ordinary ``sed --in-place … f.py`` before a refused push reported no lost write.

    For sed and perl every short option spelled with an ``i`` IS the in-place one,
    so membership in the cluster is the right test rather than a position. Scanning
    stops at ``--`` so ``sed -- s/-i/x/ f`` is not a match.
    """
    if seg.exe not in ("sed", "perl"):
        return False
    for arg in seg.argv[1:]:
        if arg == "--":
            return False
        if arg == "--in-place" or arg.startswith("--in-place="):
            return True
        cluster = _short_cluster(arg)
        if cluster and "i" in cluster:
            return True
    return False


# NOTE: an earlier revision had a `_program_comes_from_a_flag` helper here, to work
# out which operands of a sed/perl command were FILES rather than the program. It is
# deliberately gone. Deciding that requires knowing which options consume a value,
# which is unbounded — the helper itself shipped a defect (it returned only a bool,
# so `-e`'s value stayed in the operand list and was reported as an edited file).
# `_command_text` shows the command instead, which needs no such knowledge.


def _plausible_target(target: str) -> str | None:
    """A redirect target cleaned up for display, or None if it cannot be a filename.

    `shell_parse` does not understand heredocs: the BODY of a `<<EOF` block is
    parsed as if each line were a command, so a `>` occurring in ordinary prose
    inside a heredoc yields a phantom redirect whose "target" is a run of body
    text. MEASURED on the block corpus: one such phantom was a 500-character blob
    spanning newlines. Naming that in a refusal is worse than saying nothing, so
    anything that cannot be a path is dropped rather than displayed.
    """
    cleaned = target.strip().strip("\"'")
    if not cleaned or len(cleaned) > 120:
        return None
    if any(ch in cleaned for ch in ("\n", "\r", "\t")):
        return None
    return cleaned


def _command_text(seg) -> str:
    """The segment's own command text, bounded — NOT a derived list of its files.

    Deriving which operands are FILES means modelling each tool's option grammar:
    which flags cluster, which consume a value, which are program inputs. That is
    argv-to-EFFECT mapping, and it has no closed boundary — two review rounds
    demonstrated it here. Round 1 found the in-place SPELLINGS (`--in-place`,
    `-ibak`, `-Ei`, `perl -pi`); the fix for it then reported `-e`'s VALUE as an
    edited file, so `sed -i -e s/a/b/ f.py` named `s/a/b/` as a lost file, and six
    `-e` expressions would push the real file past the display cap entirely.

    The raw text is accurate under every flag, including ones invented tomorrow,
    and it is what the reader has to re-run anyway. It costs some precision — the
    note says which COMMAND was discarded rather than which file it touched — and
    that is the right trade for a cosmetic note whose whole value is being
    trustworthy.
    """
    text = " ".join(seg.raw.split())  # collapse newlines/runs; raw may span lines
    return text[:_MAX_PHRASE] + "…" if len(text) > _MAX_PHRASE else text


def _describe(seg) -> str | None:
    """A short human phrase for what this segment writes, or None if it writes nothing.

    Redirect targets and command-side writes are COMBINED, never alternatives. An
    early return on `seg.writes` meant `cp a b 2>err.log` reported only `err.log`
    and stayed silent about the copy — masking the more important write precisely
    when stderr was redirected, which is common.
    """
    parts: list[str] = []
    # Redirect targets come from the parser exactly, so these ARE named individually.
    targets = [t for t in (_plausible_target(w) for w in seg.writes) if t]
    parts.extend(targets[:_MAX_NAMED])

    if _in_place_edit(seg) or seg.exe in _WRITE_EXES:
        parts.append(_command_text(seg))
    elif _inline_script(seg):
        # A category, not an operand list — the script is inline, so there is no
        # filename to name and no grammar to model.
        parts.append(f"an inline {seg.exe} script")

    if not parts:
        return None
    # dict.fromkeys de-duplicates while PRESERVING order — a redirect target and the
    # command text can coincide.
    return ", ".join(list(dict.fromkeys(parts))[:_MAX_NAMED])


def discarded_writes(command: str) -> list[str]:
    """Every write the given command was carrying, as human phrases.

    Empty when the command is unparseable, is a single step (nothing was carried
    ALONGSIDE the refused step), or writes nothing. Never raises.
    """
    try:
        if not command or not command.strip():
            return []
        # Bounded BEFORE parsing — both checks are O(n) scans, so an adversarial
        # command costs a scan rather than a superlinear parse. Past the bound the
        # answer is "no note", the same fail-open this takes for anything it cannot
        # read. See the constants for why these values.
        if len(command) > _MAX_COMMAND_CHARS:
            return []
        if command.count("$(") + command.count("`") > _MAX_SUBSTITUTIONS:
            return []
        segs = analyze(command)
        # A redirection with NO command (`> f && git push`) writes but EXECUTES
        # nothing, so `analyze` deliberately does not yield it — doing so broke the
        # invariant its consumers rely on. Those orphan writes are still real, and
        # are exactly the targets no executed segment claims.
        claimed = {t for seg in segs for t in seg.writes}
        orphans = [t for t in write_targets(command) if t not in claimed]

        # ONE thing in the call means the refused step IS the whole call, so there is
        # no collateral to report. Counted over segments AND orphan writes together:
        # `> f && git push` is one segment plus one orphan, which is two things, while
        # a lone `> f` is one thing and stays silent like any other single step.
        if len(segs) + len(orphans) < 2:
            return []

        found: list[str] = []
        for seg in segs:
            phrase = _describe(seg)
            if phrase and len(phrase) > _MAX_PHRASE:  # one long `cp` must not dominate
                phrase = phrase[:_MAX_PHRASE] + "…"
            if phrase and phrase not in found:
                found.append(phrase)
        for target in orphans:
            named = _plausible_target(target)
            if named and not any(named in phrase for phrase in found):
                found.append(named)
        return found[:_MAX_NAMED]
    except Exception:  # noqa: BLE001 — cosmetic: never break the guard that called us
        return []


def note(command: str) -> str | None:
    """The full note to print beside a refusal, or None when there is nothing to say."""
    writes = discarded_writes(command)
    if not writes:
        return None
    return (
        "\nNOTE: the ENTIRE command was discarded, not just the step refused above.\n"
        "These writes did NOT happen:\n"
        + "".join(f"  - {w}\n" for w in writes)
        + "Re-run them once the block above is resolved, KEEPING whatever guarded\n"
        "them in the original command — a write behind a `&&` test was conditional,\n"
        "and re-running it alone can perform it in a state the original would have\n"
        "skipped."
    )


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
    re-deriving write detection in bash.

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
