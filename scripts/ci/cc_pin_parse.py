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
    pattern = re.compile(
        _ASSIGNMENT.format(var=re.escape(var), value=value_pattern),
        re.MULTILINE,
    )
    matches = pattern.findall(_strip_comments(text))
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
