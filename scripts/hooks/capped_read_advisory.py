#!/usr/bin/env python3
"""PreToolUse advisory: a ``gh`` listing is ALREADY capped before you ask.

ADVISORY ONLY. Exit 0 always, never blocks, fails open. The enforcement points
for bad counts are the reader's own discipline (CLAUDE.md, "A truncated listing
is not absence") -- this hook exists because that rule sits on the READER end
and asks you to notice an under-read at exactly the moment its own closing
sentence says you will not, because an under-read is indistinguishable from a
clean result.

THE DEFECT THIS EXISTS FOR (measured 2026-09-14): a session ran
``gh pr list --limit 30``, got exactly 30 rows, and reported "the repo has 30
open PRs". The real number was 78. It had not chosen a small number -- ``gh pr
list`` DEFAULTS to ``--limit 30``, so the command was behaviourally identical to
passing no flag at all. That is the case with no cue in it: when you type a
limit you know you limited something, and when you do not, nothing in the
command tells you a cap is already in force.

So this fires on the UNFLAGGED listing, which is the half you cannot feel, and
stays silent when you passed a limit yourself.

WHY PRE- AND NOT POST-EXECUTION. A post-hoc detector would compare the returned
record count against the limit and fire on saturation. That was measured and
rejected: across 1,168 transcripts / 79,841 Bash calls, saturation
(``n >= limit``) occurred 877 times, but 814 of those (93%) were compound
commands whose stdout is the concatenation of several commands, where the record
count means nothing and the hook would have to stay silent anyway. The
defensible fire set was 63 -- about one per twenty sessions -- for a record
counter, a JSON-shape table, an output-attribution guard and per-query dedup.
The pre-flight form needs none of that, reads no output, and has no attribution
problem, because it is a statement about the COMMAND rather than about a result.

NAMED GAPS, so this is not read as covering more than it does:
  * An explicit ``--limit`` that actually narrows or widens the read is out of
    scope -- the write-side rule in CLAUDE.md covers the limit you chose. A limit
    equal to gh's own default IS in scope, because it changed nothing.
  * A gh ALIAS that expands to a listing (``gh prs`` for ``gh pr list``) is a
    miss: the alias name is one word, so no (group, sub) resolves.
  * ``| head``, ``head -N`` and ``grep -m N`` are out of scope. Measured: they
    are 30% of all Bash calls and saturate by construction, so including them
    was a 13x noise increase for a class that is a deliberate preview.
  * SQL ``LIMIT n`` is out of scope: counting its result is unreliable across
    clients, which is the noise risk, not a claim that the semantics differ.
  * A ``gh`` call inside ``python -c`` or another embedded string is invisible
    here -- the limit is not in argv.
  * A subcommand with no entry in the defaults table is silent, never guessed.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_input import field, read_payload, session_id, session_path, tool_input  # noqa: E402
from hook_output import print_json_bounded  # noqa: E402
from shell_parse import analyze  # noqa: E402

_GENESIS_DIR = Path.home() / ".genesis"

#: Cheap prefilter before the (comparatively slow) shell parse. This hook runs
#: on EVERY Bash call, and a bare "gh" substring matches "through", "right" and
#: "high", so an untightened check sent nearly every command through analyze().
#: Excluding a non-word "gh" cannot hide a real invocation: argv[0] basename
#: would not be "gh" in those cases either.
#: NOTE the lookbehind excludes word chars and "-" but NOT "/": an absolute
#: invocation (/usr/bin/gh pr list) must still reach the parser, since
#: seg.exe resolves on the BASENAME and would match.
_GH_WORD = re.compile(r"(?<![\w-])gh(?![\w-])")

#: Per-subcommand default caps, MEASURED by reading ``gh <group> <sub> --help``
#: on gh 2.98.0 (2026-08-20) and parsing its own "(default N)". They are NOT
#: uniform -- run list is 20, workflow list 50, gist list 10 -- which is exactly
#: why they cannot be carried in anyone's head. A drift test re-reads --help and
#: fails when gh changes one, so this table is checked rather than trusted.
_GH_DEFAULT_LIMITS: dict[tuple[str, str], int] = {
    ("pr", "list"): 30,
    ("issue", "list"): 30,
    ("run", "list"): 20,
    ("release", "list"): 30,
    ("repo", "list"): 30,
    ("workflow", "list"): 50,
    ("gist", "list"): 10,
    ("cache", "list"): 30,
    ("search", "repos"): 30,
    ("search", "issues"): 30,
    ("search", "prs"): 30,
    ("search", "code"): 30,
    ("search", "commits"): 30,
}

#: gh global/value flags whose VALUE must not be mistaken for a subcommand.
#: Same hazard gh_pr_subcommand documents: the separated ``-R o/r`` form once
#: let a value be read as the verb and every downstream gate skipped the segment.
_VALUE_FLAGS = {"-R", "--repo", "--hostname", "--template", "--jq", "-q"}

#: Pagination would mean the read is NOT capped. MEASURED on gh 2.98.0: NO entry
#: in _GH_DEFAULT_LIMITS accepts either flag -- both are `gh api`-only and `gh api`
#: has no table entry -- so this branch is currently UNREACHABLE and is kept as
#: forward-compat if gh ever adds pagination to listings. An earlier comment here
#: called it "the single largest false-positive source on gh argv"; that was false.
#: Do not restore that claim without re-measuring.
_PAGINATION_FLAGS = {"--paginate", "--slurp"}

#: Dedup is per (group, sub) and the key space is EXACTLY len(_GH_DEFAULT_LIMITS),
#: so per-target dedup already bounds a session. This is a floor over that key
#: space, never a runaway brake: set BELOW the table size it silences the TAIL of
#: the table -- a session that listed 8 kinds would go permanently quiet for every
#: `gh search` subcommand, which are the listings with no independent denominator.
_MAX_FIRES_PER_SESSION = len(_GH_DEFAULT_LIMITS)


def _explicit_limit(argv: list[str]) -> int | bool | None:
    """The limit the caller set, in any spelling gh accepts.

    Returns the parsed int when recoverable, ``True`` when a limit was clearly
    set but unparseable, and ``None`` when none was set. The VALUE matters: a
    limit equal to gh's own default changed nothing, and that is precisely the
    shape of the incident this hook exists for.
    """
    toks = argv[1:]
    for i, tok in enumerate(toks):
        if tok in ("--limit", "-L"):
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            try:
                return int(nxt)
            except ValueError:
                return True
        for pre in ("--limit=", "-L="):
            if tok.startswith(pre):
                try:
                    return int(tok[len(pre) :])
                except ValueError:
                    return True
        # Glued: -L30, and -L-1. A SIGN is still an explicit limit -- an earlier
        # isdigit() check read -L-1 as "no limit set" and false-fired.
        if len(tok) > 2 and tok[:2] == "-L" and tok[2:].lstrip("+-").isdigit():
            return int(tok[2:])
    return None

def _listing_target(argv: list[str]) -> tuple[str, str] | None:
    """The ``(group, subcommand)`` of a gh listing call, else None.

    Skips flags and the values of value-taking flags, so
    ``gh --repo o/r pr list`` and ``gh pr -R o/r list`` both resolve.
    """
    if not argv or os.path.basename(argv[0]) != "gh":
        return None
    words: list[str] = []
    skip_next = False
    for tok in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        words.append(tok)
        if len(words) == 2:
            break
    if len(words) < 2:
        return None
    return words[0], words[1]


def _already_fired(sid: str, key: str) -> bool:
    """Once per (group, subcommand) per session.

    The None-path branch below is currently UNREACHABLE: session_id() already
    rejects an unsafe id and substitutes "unknown", which is a valid path
    component, so session_path never returns None here. The real behaviour for
    an id-less run is that all such invocations share one "unknown" bucket.
    Kept as a guard; NOT a live invariant to rely on.
    """
    path = session_path(_GENESIS_DIR / "sessions", sid, "capped-read-advisories")
    if path is None:
        return False
    try:
        seen = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return False
    if key in seen:
        return True
    if len(seen) >= _MAX_FIRES_PER_SESSION:
        return True
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([*seen, key]) + "\n", encoding="utf-8")
    return False


def _advisory(group: str, sub: str, cap: int, *, redundant: bool) -> str:
    """The advisory text.

    The REMEDY is as load-bearing as the detection, and it is the half nothing
    tests. An earlier draft said "pass a limit you chose or --paginate" --
    but --paginate is a `gh api`-only flag, and MEASURED on gh 2.98.0 all 13
    subcommands in the table reject it with "unknown flag: --paginate". Taking
    that advice would have replaced the model's 30 rows with an error. The
    remedy below was executed against the live tool before it shipped.
    """
    if redundant:
        lead = (
            f"[capped read] `--limit {cap}` is exactly `gh {group} {sub}`'s DEFAULT, so it "
            f"changed nothing -- this read is capped at {cap} with or without the flag."
        )
    else:
        lead = (
            f"[capped read] `gh {group} {sub}` returns at most {cap} items BY DEFAULT. "
            f"You passed no --limit, so this is already a bounded read and the command "
            f"gives you no cue that it is."
        )
    return (
        lead + "\n"
        f"If you will state a count or an absence from this result, re-run with an "
        f"explicit --limit ABOVE the number you expect and read again until the result "
        f"comes back SHORT of that limit (gh pages internally; --paginate is a `gh api` "
        f"flag that these subcommands reject). Until then the honest form is "
        f'"at least {cap}", not "{cap}".\n'
        f"For a quick look, ignore this."
    )

def _process(payload: dict) -> None:
    command = field(tool_input(payload), "command")
    if not command or not _GH_WORD.search(command):
        return
    sid = session_id(payload)
    for seg in analyze(command):
        argv = seg.argv
        if not argv or seg.exe != "gh":
            continue
        if any(t in _PAGINATION_FLAGS for t in argv):
            continue
        target = _listing_target(argv)
        if target is None:
            continue
        cap = _GH_DEFAULT_LIMITS.get(target)
        if cap is None:
            continue
        # The limit test comes AFTER the cap lookup on purpose: a limit EQUAL to
        # gh's own default is a provable no-op, and that is the literal command
        # from this hook's origin story (`gh pr list --limit 30`, reported as
        # "30 open PRs" against a true 78). Staying silent there while claiming
        # to pin that defect was an overreach the review caught.
        explicit = _explicit_limit(argv)
        # bool is a subclass of int and True == 1, so an UNPARSEABLE limit
        # (sentinel True) would read as redundant against a cap of 1. No cap
        # in the table is 1 today, which is an accident rather than a
        # guarantee -- compare only a real int.
        redundant = type(explicit) is int and explicit == cap
        if explicit is not None and not redundant:
            continue
        if _already_fired(sid, f"{target[0]}:{target[1]}"):
            continue
        print_json_bounded(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _advisory(
                        target[0], target[1], cap, redundant=redundant
                    ),
                }
            },
            text_keys=("hookSpecificOutput.additionalContext",),
        )
        return


def main() -> int:
    # Advisory: fail OPEN, always exit 0. Never run_guard (that is fail-CLOSED,
    # for irreversible guards only, and its own docstring forbids advisory use).
    with contextlib.suppress(Exception):
        _process(read_payload())
    return 0


if __name__ == "__main__":
    sys.exit(main())
