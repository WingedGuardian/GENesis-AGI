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
  * A value-taking flag placed BEFORE the leaf subcommand is a miss.
    CONFIRMED against gh 2.98.0: ``gh pr --state open list`` runs, and resolves
    here to the target ``("pr", "open")``, which has no table entry, so the hook
    stays silent. Closing it needs a per-subcommand model of gh's option grammar
    -- the open-set surface the genesis-development skill names as a tar pit --
    and the failure direction is SILENCE, which is the status quo. Left open
    deliberately.
  * A limit inside a short-flag CLUSTER is a miss in the other direction.
    CONFIRMED: ``gh pr list -dL200`` runs and really fetches 200, but ``-dL200``
    is not read as a limit, so the hook advises about a cap that is not in force.
    Costs a line of noise on a spelling with no occurrence in the 177,949-command
    replay; same open-set reasoning.
  * ``--help`` or ``--web`` appearing as a flag VALUE silences a real listing.
    MEASURED: ``gh issue list --search '--web'`` goes quiet, because the non-data
    check scans the whole argv rather than only flag POSITIONS. Bounding it needs
    to know which flags take values on which subcommand -- the same open set --
    and the spelling has no occurrence in the 177,949-command replay. Direction is
    SILENCE, recorded here so it is not a surprise.
  * ``_explicit_limit`` likewise does not skip flag VALUES, so a literal
    ``--limit`` sitting in another flag's value is read as a limit. MEASURED:
    ``gh pr list --jq --limit`` goes silent, and ``gh pr list --template -L30``
    fires claiming a default was restated when no limit was passed. Pre-existing
    and the same open set; noted because the last-wins rewrite touched this
    function without closing it.
  * A blind parse is REPORTED rather than guessed at. ``analyze_checked`` returns
    no segments at all when a bound stops the parse, so the hook would otherwise
    fall silent on exactly the commands it cannot see -- see ``_blind_advisory``.
    MEASURED over 177,949 real Bash calls: 287 gh-bearing commands (1.46% of the
    19,607 gh-bearing ones) report a blind spot, and ZERO of those were
    bounds-induced. So the silence this closes is CONSTRUCTIBLE rather than
    observed, which is why the lock on it is structural
    (``test_untokenizable_probe``) rather than a rate.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_input import field, read_payload, session_id, session_path, tool_input  # noqa: E402
from hook_output import DEFAULT_BUDGET, emit_cost, print_json_bounded  # noqa: E402
from shell_parse import BlindSpot, analyze_checked  # noqa: E402


def _envelope(context: str) -> dict:
    """The PreToolUse advisory envelope. One spelling, so it cannot drift.

    PreToolUse reaches the model ONLY through this nested shape -- a bare
    top-level ``additionalContext`` is silently discarded, and stderr on exit 0
    is read as "no objection" and never shown.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }

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

#: The GitHub Search API's hard result ceiling, which gh enforces CLIENT-SIDE.
#: MEASURED on gh 2.98.0: ``gh search prs --limit 1500`` is REFUSED outright with
#: "`--limit` must be between 1 and 1000". That breaks the generic remedy for the
#: five ``search`` rows of the table above: "re-run with a limit ABOVE the number
#: you expect" is UNFOLLOWABLE once you expect 1000 or more, so those targets need
#: their own way out (narrow the query and sum the slices) rather than a limit
#: bump gh will reject. Same failure shape as the --paginate defect below: a
#: remedy that was executed against one subcommand and generalised to thirteen.
_SEARCH_CEILING = 1000

#: Non-data execution modes. `--help` prints usage and `--web` opens a browser;
#: neither returns a CLI result whose count anyone could state, so an advisory
#: about a cap is noise AND it would burn the target's once-per-session dedup
#: slot before the first real listing.
#:
#: LONG FORMS ONLY, and that is a correctness requirement rather than a style
#: choice. The short spellings are NOT uniform across the table -- MEASURED on
#: gh 2.98.0, ``-w`` is ``--web`` on ``pr list`` / ``issue list`` / ``search prs``
#: but ``--workflow string`` on ``run list``, so treating ``-w`` as non-data would
#: silence ``gh run list -w ci.yml``, a real capped listing, in the one direction
#: this hook exists to prevent. Resolving that needs a per-subcommand model of
#: gh's short-flag grammar, which is the open-set tar pit the review's class
#: signal warned about; a closed set of unambiguous long flags needs no model.
#: The cost is a redundant advisory on the short spellings, which is noise.
_NON_DATA_FLAGS = {"--help", "--web"}

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
#: +1 for the "parse was incomplete" key, which is not a table target but shares
#: this key space. Set it to the table size exactly and that key would be the one
#: silenced, which is the same tail-silencing defect one row over.
_MAX_FIRES_PER_SESSION = len(_GH_DEFAULT_LIMITS) + 1

#: Dedup key for the blind-parse advisory. It cannot collide with a target key,
#: and the reason is stronger than the one first written here ("no gh group is
#: named with a colon", which is a fact about gh rather than about this code):
#: target keys are only ever minted for members of _GH_DEFAULT_LIMITS, since
#: _hits drops every target with no table entry. So the key space is exactly the
#: table plus this one, and a collision would require adding a literal
#: ("parse", "incomplete") row to the table.
#:
#: ONE key for all four BlindSpot causes, deliberately: an early untokenizable
#: apostrophe therefore spends the slot a later bounds-induced blind spot would
#: have used. MEASURED over 177,949 commands, 0 were bounds-induced, so the cost
#: is nil today -- recorded as a decision rather than left as an oversight.
_BLIND_KEY = "parse:incomplete"


def _explicit_limit(argv: list[str]) -> int | bool | None:
    """The limit the caller set, in any spelling gh accepts.

    Returns the parsed int when recoverable, ``True`` when a limit was clearly
    set but unparseable, and ``None`` when none was set. The VALUE matters: a
    limit equal to gh's own default changed nothing, and that is precisely the
    shape of the incident this hook exists for.

    LAST occurrence wins, because gh's flag parser (pflag) is last-value-wins.
    Returning the FIRST made ``--limit 30 --limit 200`` advise that the read was
    capped at 30 when it fetches 200 -- a factually false advisory -- while the
    reverse order stayed silent on a read that really was capped. That shape
    arises from composing a base command with a later override, not from anyone
    typing two limits on purpose.
    """
    toks = argv[1:]
    found: int | bool | None = None
    for i, tok in enumerate(toks):
        if tok in ("--limit", "-L"):
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            try:
                found = int(nxt)
            except ValueError:
                found = True
            continue
        for pre in ("--limit=", "-L="):
            if tok.startswith(pre):
                try:
                    found = int(tok[len(pre) :])
                except ValueError:
                    found = True
                break
        else:
            # Glued: -L30, and -L-1. A SIGN is still an explicit limit -- an
            # earlier isdigit() check read -L-1 as "no limit set" and
            # false-fired.
            if len(tok) > 2 and tok[:2] == "-L" and tok[2:].lstrip("+-").isdigit():
                found = int(tok[2:])
    return found

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


def _state_path(sid: str):
    """Where this session's already-advised keys live, or None if unsafe.

    The None branch is currently UNREACHABLE: session_id() already substitutes
    "unknown" for an unsafe id, and that is a valid path component. The real
    behaviour for an id-less run is that all such invocations share one
    "unknown" bucket. Kept as a guard; NOT a live invariant to rely on.
    """
    return session_path(_GENESIS_DIR / "sessions", sid, "capped-read-advisories")


def _fired_keys(sid: str) -> set[str]:
    """Read the already-advised keys. PURE -- it records nothing.

    Separating the READ from the WRITE is the whole point. The previous
    ``_already_fired`` did both in one call, so a key was recorded at the moment
    it was CONSIDERED rather than when its advisory was actually delivered. That
    is a permanent silence whenever the two diverge, and they did: MEASURED, a
    command carrying all 13 table targets plus an unparseable span recorded 14
    keys while the bounded writer trimmed the 14th block away entirely, after
    which every genuinely blind command in that session was silent.
    """
    path = _state_path(sid)
    if path is None or not path.exists():
        return set()
    try:
        return set(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return set()


def _record(sid: str, keys: list[str]) -> None:
    """Record ONLY the keys whose advisory actually went out."""
    path = _state_path(sid)
    if path is None or not keys:
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _fired_keys(sid)
        path.write_text(
            "\n".join([*existing, *(k for k in keys if k not in existing)]) + "\n",
            encoding="utf-8",
        )


def _cap_summary() -> str:
    """The defaults table as prose, DERIVED rather than restated.

    A second hand-written copy of these numbers is a copy the drift test does
    not check: it re-reads ``gh --help`` against ``_GH_DEFAULT_LIMITS`` only, so
    a hardcoded sentence would keep naming a cap that had already moved. Deriving
    it puts both copies behind the one detector.
    """
    by_cap: dict[int, list[str]] = {}
    for (group, _sub), cap in _GH_DEFAULT_LIMITS.items():
        groups = by_cap.setdefault(cap, [])
        if group not in groups:
            groups.append(group)
    return ", ".join(
        f"{'/'.join(groups)} {cap}" for cap, groups in sorted(by_cap.items(), reverse=True)
    )


def _advisory(group: str, sub: str, cap: int, *, redundant: bool) -> str:
    """The advisory text.

    The REMEDY is as load-bearing as the detection, and it is the half nothing
    tests. An earlier draft said "pass a limit you chose or --paginate" --
    but --paginate is a `gh api`-only flag, and MEASURED on gh 2.98.0 all 13
    subcommands in the table reject it with "unknown flag: --paginate". Taking
    that advice would have replaced the model's 30 rows with an error.

    TWO further remedy defects, from the same root cause and found the same way
    (by executing the text rather than reading it), are fixed here:

    * The remedy was executed against ``gh pr list`` and generalised to all
      thirteen rows. It does not hold for the five ``search`` rows, whose
      --limit is refused above :data:`_SEARCH_CEILING` -- so they get their own
      escape rather than advice gh will reject.
    * "the honest form is 'at least N'" was stated UNCONDITIONALLY. It is only
      true of a SATURATED read: a repository with five open PRs returns five,
      which is exactly five and not "at least thirty". A short result is the
      true count, and saying otherwise teaches a false hedge.

    THE STANDARD THAT REPLACES "executed against the live tool", which is what
    the removed sentence claimed while only one subcommand had ever been tried:
    every branch of this text is executed against EVERY family it addresses.
    MEASURED on gh 2.98.0, 2026-09-15 -- ``gh search prs --limit 1000`` returns
    exactly 1000 (saturated) and ``--limit 1001`` is refused, so the generic
    remedy is unfollowable there; and the escape this text prescribes instead
    was RUN, three ``--created`` date slices each returning short of its own
    limit and therefore provably complete.
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
    if group == "search":
        escape = (
            f"`gh search` cannot be widened past {_SEARCH_CEILING}: the API caps results "
            f"there and gh REFUSES a larger flag outright (\"`--limit` must be between 1 "
            f"and {_SEARCH_CEILING}\"). So exactly {_SEARCH_CEILING} back means INCOMPLETE "
            f"with no larger limit available -- narrow the query (by date range, owner or "
            f"repo) and sum the slices."
        )
    else:
        escape = (
            "gh pages internally, so a limit you choose is honoured; --paginate is a "
            "`gh api` flag that these subcommands reject."
        )
    return (
        lead + "\n"
        "If you will state a count or an absence from this result, re-run with an "
        "explicit --limit ABOVE the number you expect. A result SHORTER than the limit "
        "you passed is the TRUE count; a result of exactly the limit means there may be "
        "more, so raise it and read again. " + escape + "\n"
        f"Reading exactly {cap} back from THIS command therefore supports "
        f'"at least {cap}", never "{cap}"; fewer than {cap} is exact.\n'
        f"For a quick look, ignore this."
    )

def _blind_advisory(blind: BlindSpot, *, found_any: bool) -> str:
    """Said when the parse could not see the whole command.

    ``analyze_checked`` returns NO segments at all when a BOUND stopped the parse,
    so on that path this hook would otherwise go silent -- and silence is the exact
    failure it exists to prevent: an unflagged capped read. Bare ``analyze`` cannot
    even report the condition, which is why importing it is refused by
    ``test_untokenizable_probe.TestNoConsumerCanSkipTheChokepoint``.

    ``found_any`` is not cosmetic. Two of the four blind causes return segments
    alongside the blind spot, so this block and a per-target block are emitted
    TOGETHER -- and saying "I could not check whether it contains a gh listing"
    directly beneath a block that just named one and stated its cap is a message
    contradicting itself in one breath. The claim that survives both cases is
    about what else might be there.
    """
    scope = "another `gh` listing" if found_any else "a `gh` listing"
    return (
        f"[capped read] This command {blind.cause}, so I could not check whether it "
        f"contains {scope} -- and every gh listing is capped by default "
        f"({_cap_summary()}).\n"
        f"If you will state a count or an absence from its output, pass an explicit "
        f"--limit ABOVE the number you expect and treat a result of exactly that "
        f"limit as incomplete.\n"
        f"For a quick look, ignore this."
    )


def _hits(command: str) -> tuple[list[tuple[str, str, int, bool]], object]:
    """Every capped listing in the command, plus why the parse may be incomplete.

    Returns ALL targets rather than the first. A single Bash call such as
    `gh pr list; gh run list` carries two DIFFERENT caps (30 and 20), and
    returning after the first mentioned only one while never even recording the
    second -- so a count drawn from the run listing got no warning at all.
    """
    segments, blind = analyze_checked(command)
    found: list[tuple[str, str, int, bool]] = []
    for seg in segments:
        argv = seg.argv
        if not argv or seg.exe != "gh":
            continue
        if any(t in _PAGINATION_FLAGS for t in argv):
            continue
        if any(t in _NON_DATA_FLAGS for t in argv):
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
        found.append((target[0], target[1], cap, redundant))
    return found, blind


def _process(payload: dict) -> None:
    command = field(tool_input(payload), "command")
    if not command or not _GH_WORD.search(command):
        return
    sid = session_id(payload)
    found, blind = _hits(command)

    seen = _fired_keys(sid)
    pending: list[tuple[str, str]] = []
    for group, sub, cap, redundant in found:
        key = f"{group}:{sub}"
        if key in seen or len(seen) >= _MAX_FIRES_PER_SESSION:
            continue
        seen.add(key)
        pending.append((key, _advisory(group, sub, cap, redundant=redundant)))
    # A blind parse is reported EVEN WHEN a listing was found, because "found
    # something and stopped looking" is precisely what the chokepoint exists to
    # surface: a second listing past the bound would otherwise go unmentioned
    # under cover of an advisory about the first.
    blind_block: tuple[str, str] | None = None
    if blind is not None and _BLIND_KEY not in seen and len(seen) < _MAX_FIRES_PER_SESSION:
        seen.add(_BLIND_KEY)
        blind_block = (_BLIND_KEY, _blind_advisory(blind, found_any=bool(found)))
    if not pending and blind_block is None:
        return

    # Select WHOLE blocks under the budget, rather than letting the writer trim
    # across a block boundary. A key recorded for a block nobody saw is a
    # PERMANENT silence for that key, and the writer trims from the tail, so the
    # last block is destroyed entirely while its key sits on disk. The blind
    # block is RESERVED first: losing "I could not read this command" is worse
    # than losing one target's cap reminder, and it is the cheapest block there
    # is. Whatever does not fit is simply not recorded, so a later command
    # carrying the same target still gets its advisory.
    overhead = emit_cost(json.dumps(_envelope("")))
    room = DEFAULT_BUDGET - overhead - (emit_cost(blind_block[1]) + 2 if blind_block else 0)
    kept: list[tuple[str, str]] = []
    for key, text in pending:
        cost = emit_cost(text) + 2
        if cost > room:
            break
        room -= cost
        kept.append((key, text))
    if blind_block is not None:
        kept.append(blind_block)

    print_json_bounded(
        _envelope("\n\n".join(text for _key, text in kept)),
        text_keys=("hookSpecificOutput.additionalContext",),
    )
    _record(sid, [key for key, _text in kept])


def main() -> int:
    # Advisory: fail OPEN, always exit 0. Never run_guard (that is fail-CLOSED,
    # for irreversible guards only, and its own docstring forbids advisory use).
    with contextlib.suppress(Exception):
        _process(read_payload())
    return 0


if __name__ == "__main__":
    sys.exit(main())
