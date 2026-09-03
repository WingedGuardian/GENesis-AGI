"""Read a pin out of ``scripts/lib/cc_version.sh`` the way bash reads it.

Every CI guard over the Claude Code pin has to answer one question first: what
value does the runtime actually use? Getting that wrong makes the guard check a
number nobody runs, which is worse than having no guard — it reports green over
the thing it was built to catch.

THE BUG THIS EXISTS TO FIX. The guards each carried a copy of

    re.compile(r'CC_VERSION="?\\$\\{CC_VERSION:-([0-9]+\\.[0-9]+\\.[0-9]+)\\}"?')

and called ``.search()`` on the whole file. Two divergences from bash follow:

  * ``search`` returns the FIRST match anywhere in the file. Bash executes
    assignments in order, so the value that survives is the LAST one.
  * The pattern is unanchored, so it matches inside a ``#`` comment — a line
    bash never executes at all.

Together those let one comment line above the real pin decide what a guard
believes. Measured against ``bash -c '. cc_version.sh; echo $CC_VERSION'``:

    # was: CC_VERSION="${CC_VERSION:-2.1.218}"     <- a comment
    CC_VERSION="${CC_VERSION:-2.1.246}"            <- what bash uses

  check_cc_node_lockstep.parse_pins  ->  2.1.218   (and Node 18 from a decoy)
  bash                               ->  2.1.246

so the Node-floor guard could be pointed at a pin the machine never installs,
and the pin-receipt guard could be told "unchanged" for a PR that bumps it.

WHAT THIS DOES INSTEAD. Comment lines are dropped, assignments must start their
line, and every match is collected rather than the first. Then:

  * exactly one  -> that value
  * none         -> ``None``
  * more than one-> ``None``

RECOGNISING THE PIN FORM IS NOT KNOWING WHAT THE FILE MEANS. ``cc_version.sh``
is sourced, not read, so a line the pin pattern does not recognise can still
change the value. A file may leave the pin untouched and reassign below it::

    CC_VERSION="${CC_VERSION:-2.1.218}"
    if [ -z "${CC_ALIGN_LEGACY:-}" ]; then
      CC_VERSION="2.1.999"
    fi

Reading only the pin form gives 2.1.218 for a file bash resolves as 2.1.999 —
a forward move to an un-vetted version that reads as "unchanged" and is asked
for no receipts. So the count that decides is over EVERY assignment to the
variable (``_ANY_ASSIGNMENT``), in any shape, taken before any value is read.
More than one means the file's value is not something this parser can state.

Returning ``None`` for a duplicate is deliberate, and it is NOT what bash does
(bash takes the last). A second effective assignment in a one-line pin file is
not a thing anyone does by accident, so the honest report is "I cannot tell you
what this file means", and the caller fails closed. Silently taking the last
would let a PR hide a pin change behind a plausible-looking earlier assignment.

DELIBERATELY NOT SOURCING THE FILE. ``bash -c '. cc_version.sh; echo …'`` would
be exact, and is exactly the wrong instrument: on a pull_request the file is
attacker-controlled, so sourcing it executes a contributor's shell in CI. A
parser that under-claims is the correct trade.

Callers must treat ``None`` as a BLOCK, never as a skip. It means the file did
not parse — not that there is nothing to check.
"""

from __future__ import annotations

import re

#: ``CC_VERSION="${CC_VERSION:-2.1.201}"`` at the start of a line. Quoting and
#: spacing around the assignment are tolerated; a leading ``#`` is not, because
#: ``_strip_comments`` has already removed those lines.
_ASSIGNMENT = r'^[^\S\n]*{var}=[^\S\n]*"?\$\{{{var}:-{value}\}}"?'

#: ANY assignment to the variable, in any shape — not just the pin form above.
#: This exists because recognising only the pin form is not the same as knowing
#: what the file MEANS. A file can leave the pin line untouched and reassign the
#: variable further down::
#:
#:     CC_VERSION="${CC_VERSION:-2.1.218}"     <- the only line _ASSIGNMENT sees
#:     if [ -z "${CC_ALIGN_LEGACY:-}" ]; then
#:       CC_VERSION="2.1.999"                  <- what bash actually resolves
#:     fi
#:
#: Matching only the pin form reports 2.1.218 for a file bash resolves as
#: 2.1.999 — a forward move to an un-vetted version that reads as "unchanged"
#: and is asked for no receipts. Counting EVERY assignment closes that: two
#: means the file's value is not a thing this parser can state.
#:
#: Deliberately over-broad. Over-detection costs a block (safe: a human reads
#: the file); under-detection costs a bypass. This is a DETECTOR, not a shell
#: parser — it never has to decide which assignment wins, only whether more
#: than one exists.
#:
#: NOT ANCHORED, and that is the whole point. An anchored version of this check
#: shipped first and closed exactly one shape — the one it had a test for. Bash
#: executes assignments after ``;``, ``||`` and ``&&``, and inside one-line
#: ``if``/``case`` bodies, none of which start a line. MEASURED, every one of
#: these left the guard reporting the untouched pin line while bash resolved
#: 2.1.999::
#:
#:     [ -n "${X:-}" ] || CC_VERSION="2.1.999"
#:     if true; then CC_VERSION="2.1.999"; fi
#:     true && CC_VERSION=2.1.999
#:     printf -v CC_VERSION "2.1.999"
#:     read -r CC_VERSION <<< "2.1.999"
#:     eval CC_VERSION=2.1.999
#:
#: The lookbehind keeps ``MY_CC_VERSION=`` and ``OLD_CC_VERSION=`` from counting.
#: ``"${CC_VERSION:-…}"`` is not miscounted either: that text is ``CC_VERSION:-``,
#: not ``CC_VERSION=``. The last three alternatives cover the builtins that
#: assign without an ``=`` at all.
_ANY_ASSIGNMENT = (
    r"(?<![\w.-]){var}="
    r"|printf[^\n]*-v[^\S\n]+{var}\b"
    r"|read[^\n]*[^\S\n]{var}\b"
    r"|eval\b[^\n]*{var}"
)

_SEMVER = r"([0-9]+\.[0-9]+\.[0-9]+)"
_INTEGER = r"([0-9]+)"


def _strip_comments(text: str) -> str:
    """Drop whole-line ``#`` comments.

    Only whole-line comments: a trailing ``# note`` after a real assignment
    leaves the assignment intact, and the anchored pattern reads it correctly.
    Trying to strip trailing comments properly would mean tracking shell
    quoting, which is the hand-rolled-shell-parsing trap — and it buys nothing
    here, because the value is captured before any ``#`` could appear.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _parse(text: str, var: str, value_pattern: str) -> str | None:
    body = _strip_comments(text)

    # Refuse BEFORE reading a value: if the variable is assigned more than once
    # in any shape, the pin line is not the file's answer and extracting it
    # would state a version bash does not resolve to.
    if len(re.findall(_ANY_ASSIGNMENT.format(var=re.escape(var)), body, re.MULTILINE)) != 1:
        return None

    pattern = re.compile(
        _ASSIGNMENT.format(var=re.escape(var), value=value_pattern),
        re.MULTILINE,
    )
    matches = pattern.findall(body)
    if len(matches) != 1:
        return None  # 0 = not found; >1 = ambiguous. Both are "cannot tell".
    return matches[0]


def parse_cc_version(text: str) -> str | None:
    """The pinned Claude Code version, or ``None`` if it cannot be determined."""
    return _parse(text, "CC_VERSION", _SEMVER)


def parse_node_major(text: str) -> int | None:
    """The pinned Node major, or ``None`` if it cannot be determined."""
    raw = _parse(text, "NODE_MAJOR", _INTEGER)
    return int(raw) if raw is not None else None
