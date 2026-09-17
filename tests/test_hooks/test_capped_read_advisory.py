"""The capped-read advisory: fires on the limit you did NOT set.

The defect being pinned (measured 2026-09-14): a session ran ``gh pr list
--limit 30``, got 30 rows, and reported 30 as the repo's open-PR count. The real
number was 78 -- and ``gh pr list`` defaults to 30, so the command was identical
to passing no flag. The hook targets the unflagged form, which is the half that
carries no cue.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
HOOK = REPO / "scripts/hooks/capped_read_advisory.py"
PY = sys.executable


def _run(command: str, tmp: Path, *, session: str = "s1") -> dict | None:
    """Drive the REAL hook as a subprocess, hermetic HOME/TMPDIR for dedup state."""
    payload = {
        "tool_name": "Bash",
        "session_id": session,
        "tool_input": {"command": command},
    }
    proc = subprocess.run(
        [PY, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp), "HOME": str(tmp)},
    )
    assert proc.returncode == 0, f"advisory hook must never block: {proc.stderr}"
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _context(out: dict | None) -> str:
    assert out is not None, "hook produced no output"
    return out["hookSpecificOutput"]["additionalContext"]


# --------------------------------------------------------------------------
# The acceptance bar: the real defect.
# --------------------------------------------------------------------------


def test_bare_gh_pr_list_fires_and_names_the_default(tmp_path: Path) -> None:
    """THE acceptance case. Mutation: delete _GH_DEFAULT_LIMITS -> no fire."""
    out = _run("gh pr list --repo o/r --state open --json number", tmp_path)
    ctx = _context(out)
    assert "30" in ctx
    assert "gh pr list" in ctx


def test_the_literal_defect_command_fires(tmp_path: Path) -> None:
    """`gh pr list --limit 30` -- THE command from the incident.

    30 IS gh pr list's default, so the flag changed nothing; the session that
    typed it reported 30 as the repo's open-PR count against a true 78. An
    earlier revision stayed silent here while the docstring, changelog and skill
    all claimed to pin this defect. Mutation: restore the unconditional
    `if explicit is not None: continue` -> RED.
    """
    ctx = _context(_run("gh pr list --limit 30", tmp_path))
    assert "default" in ctx.lower()
    assert "changed nothing" in ctx


def test_remedy_does_not_prescribe_a_flag_the_tool_rejects(tmp_path: Path) -> None:
    """The REMEDY is a separate claim from the detection, and only one gets tested.

    An earlier advisory said "pass a limit you chose or --paginate". MEASURED on
    gh 2.98.0: all 13 table subcommands answer `unknown flag: --paginate` -- it
    is `gh api`-only. Following that advice would have replaced the model's rows
    with an error. This pins the corrected wording.
    """
    ctx = _context(_run("gh pr list", tmp_path))
    assert "--limit" in ctx, "must prescribe the flag that actually works"
    # --paginate may only be MENTIONED as the thing not to use.
    for line in ctx.splitlines():
        if "--paginate" in line:
            assert "reject" in line, f"--paginate must be named as rejected, got: {line}"


def test_default_is_per_subcommand_not_uniform(tmp_path: Path) -> None:
    """run list is 20, not 30 -- the reason the table cannot live in a head.

    Mutation: collapse the table to a single constant -> this goes RED.
    """
    assert "20" in _context(_run("gh run list --branch main", tmp_path))
    assert "50" in _context(_run("gh workflow list", tmp_path, session="s2"))


def test_advisory_offers_a_dismissal_and_a_sayable_form(tmp_path: Path) -> None:
    """The wording claims are testable, not aspirational.

    Correct for a top-N sample (dismissable) AND gives a correct utterance to
    adopt. Mutation: drop either clause -> RED.
    """
    ctx = _context(_run("gh pr list", tmp_path))
    assert "at least 30" in ctx, "must offer the honest form to say"
    assert "ignore this" in ctx.lower(), "must be dismissable for a quick look"


# --------------------------------------------------------------------------
# Silence: every one of these is a way the hook could become noise.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gh pr list --limit 200",  # caller chose a limit that WIDENS
        "gh pr list --limit=200",
        "gh pr list -L 200",
        "gh pr list -L200",  # glued short form
        "gh pr list --limit 5",  # a limit that NARROWS is still a choice
        "gh pr list -L-1",  # a SIGN is still an explicit limit (once false-fired)
        "gh api --paginate repos/o/r/pulls --per-page 100",  # not capped
        "gh pr list --slurp",
        "git worktree list",  # not gh at all
        "gh pr view 1850 --json state",  # not a listing
        "gh secret list",  # no measured default -> never guess
        "echo 'gh pr list'",  # a mention, not an invocation
    ],
)
def test_stays_silent(command: str, tmp_path: Path) -> None:
    assert _run(command, tmp_path) is None, f"should not fire: {command}"


def test_a_gh_listing_piped_to_head_still_fires(tmp_path: Path) -> None:
    """The cap is still in force when the output is piped onward.

    This REPLACES a vacuous test. The old one asserted silence for
    `cat big.log | head -30` and `grep -m 30 ...` -- neither contains a
    standalone `gh`, so both were rejected at the prefilter and the test passed
    with the entire hook body deleted. It locked nothing. The real scope claim
    worth pinning is this one: a `head` on the END of a gh listing does not undo
    gh's own cap, so the advisory is still correct and still fires.
    """
    assert "30" in _context(_run("gh pr list | head -5", tmp_path))


# --------------------------------------------------------------------------
# argv walking: the value-flag hazard gh_pr_subcommand documents.
# --------------------------------------------------------------------------


def test_global_flag_before_the_group_still_resolves(tmp_path: Path) -> None:
    assert "30" in _context(_run("gh --repo o/r pr list", tmp_path))


def test_value_flag_between_group_and_sub_still_resolves(tmp_path: Path) -> None:
    """``gh pr -R o/r list`` -- the value must not be read as the subcommand.

    Mutation: stop skipping _VALUE_FLAGS values -> 'o/r' is read as the
    subcommand, no table entry, silent -> RED.
    """
    assert "30" in _context(_run("gh pr -R o/r list", tmp_path))


def test_absolute_path_invocation_still_resolves(tmp_path: Path) -> None:
    """/usr/bin/gh pr list -- the prefilter must not exclude a "/"-prefixed gh.

    seg.exe resolves on the basename, so excluding it in the cheap prefilter
    would silently narrow the hook. Mutation: add "/" back to the lookbehind
    exclusion -> RED.
    """
    assert "30" in _context(_run("/usr/bin/gh pr list", tmp_path))


def test_search_group_resolves(tmp_path: Path) -> None:
    assert "30" in _context(_run("gh search repos genesis --sort stars", tmp_path))


# --------------------------------------------------------------------------
# Dedup and runaway cap.
# --------------------------------------------------------------------------


def test_same_target_fires_once_per_session(tmp_path: Path) -> None:
    assert _run("gh pr list", tmp_path, session="dup") is not None
    assert _run("gh pr list --state open", tmp_path, session="dup") is None


def test_a_different_target_still_fires_in_the_same_session(tmp_path: Path) -> None:
    """Dedup is per (group, sub) -- it must not silence a DIFFERENT listing."""
    assert _run("gh pr list", tmp_path, session="two") is not None
    assert _run("gh run list", tmp_path, session="two") is not None


def test_dedup_is_per_session(tmp_path: Path) -> None:
    assert _run("gh pr list", tmp_path, session="sess-a") is not None
    assert _run("gh pr list", tmp_path, session="sess-b") is not None


# --------------------------------------------------------------------------
# Contract: advisory, fail-open, correct channel.
# --------------------------------------------------------------------------


def test_emits_the_nested_envelope_not_a_bare_key(tmp_path: Path) -> None:
    """A bare top-level additionalContext is SILENTLY DISCARDED by CC.

    Four hooks shipped inert that way; test_advisory_channel.py exists for it.
    """
    out = _run("gh pr list", tmp_path)
    assert out is not None
    assert "additionalContext" not in out, "top-level key is discarded by CC"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


@pytest.mark.parametrize(
    "raw",
    ['{"tool_name":"Bash"}', "not json at all", "", '{"tool_input":{"command":null}}', "[]"],
)
def test_fails_open_on_garbage(raw: str, tmp_path: Path) -> None:
    """Never crash the session on a hostile or malformed payload.

    HONEST SCOPE, recorded because a mutation sweep proved it: removing the
    contextlib.suppress in main() does NOT make this test fail. That is not a
    vacuous test -- it is a behaviourally-null mutation. read_payload() is
    fail-open BY CONTRACT (any parse failure yields {}), and shell_parse.analyze
    is documented never to crash, so no payload reaching _process through this
    surface raises in the first place. What this test proves is the observable
    contract: exit 0, no traceback, on every malformed shape. The suppress is
    defence-in-depth against FUTURE code in _process that can raise, and it is
    not provable from outside today -- do not delete it on the strength of a
    surviving mutation.
    """
    proc = subprocess.run(
        [PY, str(HOOK)],
        input=raw,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert "Traceback" not in proc.stderr


# --------------------------------------------------------------------------
# Drift: the table is checked against the tool, not frozen on faith.
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("gh") is None, reason="gh not installed")
def test_default_table_matches_the_installed_gh() -> None:
    """Re-read gh's own --help and compare. A detector, not a frozen literal.

    gh owns these numbers; when it changes one, this fails loudly instead of
    the hook quietly naming a cap that no longer exists.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    import re

    from capped_read_advisory import _GH_DEFAULT_LIMITS

    mismatches: list[str] = []
    unverified: list[str] = []
    checked = 0
    for (group, sub), expected in _GH_DEFAULT_LIMITS.items():
        proc = subprocess.run(
            ["gh", group, sub, "--help"], capture_output=True, text=True, timeout=30
        )
        m = re.search(r"--limit int[^\n]*\(default (\d+)\)", proc.stdout + proc.stderr)
        if m is None:
            # Absent from THIS gh build, or its help stopped advertising a
            # default. An environment fact, not drift -- failing here would
            # block every PR on a runner whose gh differs.
            unverified.append(f"{group} {sub} (rc={proc.returncode})")
            continue
        checked += 1
        if int(m.group(1)) != expected:
            mismatches.append(f"gh {group} {sub}: table says {expected}, gh says {m.group(1)}")
    assert not mismatches, "defaults table has drifted from the installed gh:\n" + "\n".join(
        mismatches
    )
    # A run that verified nothing is NOT a pass -- it is a detector that stopped
    # detecting, which is exactly what the environment escape above could hide.
    assert checked >= len(_GH_DEFAULT_LIMITS) // 2, (
        f"drift check verified only {checked}/{len(_GH_DEFAULT_LIMITS)} entries; "
        f"unverified: {unverified}"
    )


# --------------------------------------------------------------------------
# The blind parse. CI caught this one, not a reviewer: the hook imported bare
# `analyze`, which returns [] both for "no gh listing here" and for "a bound
# stopped me looking" -- so the shape below made it go SILENT, which is the
# exact failure the hook exists to prevent.
# --------------------------------------------------------------------------


def test_a_bound_that_blinds_the_parser_still_advises(tmp_path: Path) -> None:
    """MEASURED: over MAX_COMMAND_CHARS, analyze_checked returns ZERO segments.

    Bare `analyze` returns [] here too and cannot say why, so the old code fell
    through its loop and emitted nothing. Mutation (import `analyze`, drop the
    blind branch) -> no output -> BITES.
    """
    out = _run("gh pr list --json number " + "#" * 60_000, tmp_path)
    ctx = _context(out)
    assert "capped read" in ctx
    assert "49152" in ctx or "longer than" in ctx


def test_the_blind_advisory_claims_only_what_it_knows(tmp_path: Path) -> None:
    """It must not assert a listing IS present -- it could not check."""
    ctx = _context(_run("gh pr list " + "#" * 60_000, tmp_path))
    assert "could not be read" in ctx
    assert "may have missed" in ctx
    # Still actionable -- but the action is a CHECK, never a prescribed flag,
    # and never "a short result proves it".
    assert "reports a TOTAL" in ctx
    assert "do NOT take a short result as proof" in ctx


def test_a_blind_parse_is_reported_even_when_a_listing_was_found(
    tmp_path: Path,
) -> None:
    """ "Found something AND stopped looking" is the case worth surfacing.

    An untokenizable command still yields segments, so the per-target advisory
    fires -- and a second listing past the blind spot would go unmentioned under
    cover of it unless the incompleteness is said out loud.
    """
    ctx = _context(_run('gh pr list --search "unbalanced', tmp_path))
    assert "gh pr list" in ctx
    assert "could not be read" in ctx


# --------------------------------------------------------------------------
# One Bash call, several caps.
# --------------------------------------------------------------------------


def test_a_compound_warns_for_every_distinct_cap(tmp_path: Path) -> None:
    """`gh pr list; gh run list` carries TWO different caps, 30 and 20.

    Returning after the first mentioned only 30 and never recorded the run
    listing, so a count drawn from it got no warning. Mutation (return after the
    first block) -> "20" absent -> BITES.
    """
    ctx = _context(_run("gh pr list --json number; gh run list --json databaseId", tmp_path))
    assert "gh pr list" in ctx and "30" in ctx
    assert "gh run list" in ctx and "20" in ctx


def test_a_compound_does_not_repeat_one_target(tmp_path: Path) -> None:
    """Per-target dedup still holds WITHIN a single command."""
    ctx = _context(_run("gh pr list; gh pr list", tmp_path))
    assert ctx.count("[capped read] `gh pr list`") == 1


# --------------------------------------------------------------------------
# gh's flag semantics: last-value-wins, and the non-data modes.
# --------------------------------------------------------------------------


def test_the_effective_last_limit_wins(tmp_path: Path) -> None:
    """pflag is last-value-wins, so reading the FIRST --limit inverts both cases.

    Mutation (return on the first match) -> the widened case fires a FALSE
    advisory and the capped case goes silent -> BITES in both directions.
    """
    # Widened last: really fetches 200, so there is nothing to warn about.
    assert _run("gh pr list --limit 30 --limit 200", tmp_path) is None
    # Restated-default last: really capped at 30, and must fire.
    ctx = _context(_run("gh pr list --limit 200 --limit 30", tmp_path, session="s2"))
    assert "30" in ctx


@pytest.mark.parametrize("flag", ["--help", "--web"])
def test_non_data_modes_are_not_capped_reads(flag: str, tmp_path: Path) -> None:
    """`--help` prints usage and `--web` opens a browser. Neither returns rows."""
    assert _run(f"gh pr list {flag}", tmp_path) is None


def test_a_short_flag_that_is_not_web_is_never_read_as_non_data(
    tmp_path: Path,
) -> None:
    """MEASURED on gh 2.98.0: `-w` is --web on `pr list` but --workflow on

    `run list`, where it takes a VALUE. Excluding the short spelling would
    silence `gh run list -w ci.yml` -- a real capped read at 20 -- which is the
    harmful direction. The long forms are unambiguous and need no per-subcommand
    model of gh's grammar. Mutation (add "-w" to _NON_DATA_FLAGS) -> BITES.
    """
    ctx = _context(_run("gh run list -w ci.yml", tmp_path))
    assert "gh run list" in ctx
    assert "20" in ctx


def test_a_non_data_mode_does_not_burn_the_dedup_slot(tmp_path: Path) -> None:
    """The once-per-session slot belongs to the first REAL listing.

    Mutation (skip --help after the _already_fired call instead of before) ->
    the real listing that follows goes silent -> BITES.
    """
    assert _run("gh pr list --help", tmp_path) is None
    ctx = _context(_run("gh pr list", tmp_path))
    assert "30" in ctx


# --------------------------------------------------------------------------
# The REMEDY. Two defects here shipped past a 13-cell detector matrix and an
# 82k-command replay, because only the DETECTOR was ever tested.
# --------------------------------------------------------------------------


def test_search_names_the_api_ceiling_instead_of_a_bigger_limit(
    tmp_path: Path,
) -> None:
    """MEASURED on gh 2.98.0: `gh search prs --limit 1500` is REFUSED --

    "`--limit` must be between 1 and 1000". So "pass a limit above the number
    you expect" is unfollowable for this family once you expect >= 1000, and
    the generic remedy had to be executed against it rather than generalised.
    """
    ctx = _context(_run("gh search prs --owner o", tmp_path))
    assert "1000" in ctx
    assert "narrow the query" in ctx


def test_a_short_read_is_never_claimed_as_proof_of_completeness(tmp_path: Path) -> None:
    """THE PREMISE FIX. This test previously asserted the false claim by name.

    "A result SHORTER than the limit you passed is the TRUE count" was the hook's
    central sentence and it is FALSE in at least three measured ways: a filtered
    `gh run list` stops at the workflow-runs endpoint's 1,000 ceiling; `gh pr
    list --search` / `issue list --search` route through GitHub search and stop
    at 1,000; and any `gh search` that timed out reports `incomplete_results`
    while returning fewer rows than asked for. Each is a SHORT read that is not
    complete, so no amount of raising --limit establishes a count.

    Three consecutive review rounds each found another family where it failed.
    That is a premise defect, not a coverage gap, so the claim is DELETED rather
    than carved out a fourth time. What the hook can still say is true: the cap
    exists, and a SATURATED read is not a count.

    Mutation (restore any "short result is the true count" wording) -> BITES.
    """
    ctx = _context(_run("gh pr list", tmp_path))
    # What it may still claim.
    assert 'supports "at least 30", never "30"' in ctx
    assert "MORE ROWS" in ctx
    # What it must never claim again.
    assert "does not establish COMPLETENESS" in ctx
    assert "the server stopped early" in ctx
    assert "TRUE count" not in ctx
    # NOT a blunt ban on the word "exact": the remedy legitimately says a
    # total_count is exact WHEN incomplete_results is false. What must be absent
    # is the claim about a SHORT READ, so pin that, and require the qualifier
    # that makes the surviving use of "exact" true.
    assert "fewer than 30 is exact" not in ctx
    # And the ONE source it does point at must carry its own qualifier: a
    # timed-out search returns total_count NEXT TO incomplete_results: true, so
    # an unqualified "read total_count" is the same defect one level down.
    # Asserting `"incomplete_results" in ctx` was NOT enough -- the word also
    # appears earlier in the warning paragraph, so that assertion passed for the
    # wrong reason and a mutation removing the qualifier survived.
    assert "only when that flag is false" in ctx


def test_the_json_envelope_survives_the_worst_case_output(tmp_path: Path) -> None:
    """Many blocks now concatenate into ONE additionalContext, so the cap matters.

    MEASURED 2026-09-15: all 13 table targets plus the blind block cost 10,521
    units against a 9,800 budget under a 10,000-char cap -- so this path CAN
    overrun, and what must survive is the envelope, never the prose. An
    oversized advisory that loses `hookEventName` is not an advisory at all.

    No corpus command has ever carried 13 listings (0 of 177,949), so this locks
    a CONSTRUCTIBLE case rather than an observed one -- which is the point: the
    cap moves between CC versions and nothing else would notice.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS

    command = "; ".join(f"gh {group} {sub}" for group, sub in _GH_DEFAULT_LIMITS)
    out = _run(command, tmp_path)
    assert out is not None
    # The envelope, in full -- this is the part the bounded writer must never trim.
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[capped read]")
    # It fit under the cap...
    assert len(ctx) < 10_000
    # ...and it got there by dropping WHOLE blocks, never by cutting across a
    # block boundary. A half-block is a cap reminder with its remedy amputated,
    # and the key for it would have been recorded as delivered.
    assert ctx.count("[capped read]") > 1
    for block in ctx.split("\n\n"):
        # Every advisory block ends with its dismissal; the omission line is the
        # one block that does not, and it is a whole line either way.
        whole = block.rstrip().endswith("ignore this.") or block.rstrip().endswith(
            "will describe it."
        )
        assert whole, f"a block was cut mid-value rather than dropped whole: ...{block[-80:]!r}"


# --------------------------------------------------------------------------
# Round-2 review: a key recorded for a block nobody saw is a PERMANENT silence.
# --------------------------------------------------------------------------


def test_an_undelivered_block_is_never_recorded_as_delivered(tmp_path: Path) -> None:
    """The defect: RECORD and EMIT were different events, and record went first.

    MEASURED before the fix, on 13 targets plus an unparseable span: 14 keys were
    written to the dedup file while the bounded writer destroyed the 14th block
    entirely -- so the session reported a blind parse zero times, forever, in the
    one direction this hook exists to prevent.

    Mutation (record inside the selection loop instead of after the emit) -> the
    dropped target is recorded and never advised again -> BITES.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS

    targets = list(_GH_DEFAULT_LIMITS)
    command = "; ".join(f"gh {g} {s}" for g, s in targets) + ' --search "unbalanced'
    ctx = _context(_run(command, tmp_path))

    dropped = [(g, s) for g, s in targets if f"`gh {g} {s}`" not in ctx]
    assert dropped, "expected the budget to drop at least one block whole"
    # Whatever did not ship was not recorded, so it still advises on its own.
    for group, sub in dropped:
        again = _context(_run(f"gh {group} {sub}", tmp_path))
        assert f"`gh {group} {sub}`" in again, f"{group} {sub} was silently lost"


def test_the_blind_block_is_reserved_against_the_budget(tmp_path: Path) -> None:
    """Losing "I could not read this command" costs more than losing one cap.

    It is also the cheapest block there is, so reserving it is nearly free.
    Mutation (append the blind block last without reserving room) -> it is the
    one the writer destroys -> BITES.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS

    command = "; ".join(f"gh {g} {s}" for g, s in _GH_DEFAULT_LIMITS) + ' --search "unbalanced'
    ctx = _context(_run(command, tmp_path))
    assert "could not be read" in ctx
    # And nothing was cut mid-block -- whole blocks in, whole blocks out.
    assert "truncated" not in ctx


def test_the_blind_block_does_not_contradict_a_named_listing(tmp_path: Path) -> None:
    """Three of the five blind causes return segments, so both blocks co-emit.

    Saying "I could not check whether it contains a gh listing" directly under a
    block that just named one and stated its cap is a message contradicting
    itself. Mutation (drop found_any) -> BITES.
    """
    ctx = _context(_run('gh pr list --search "unbalanced', tmp_path))
    assert "`gh pr list`" in ctx
    assert "missed another `gh` listing" in ctx
    assert "missed a `gh` listing" not in ctx


def test_the_blind_block_says_a_listing_when_none_was_found(tmp_path: Path) -> None:
    """The other side of the same word -- a bound yields NO segments at all."""
    ctx = _context(_run("gh pr list " + "#" * 60_000, tmp_path))
    assert "missed a `gh` listing" in ctx
    assert "another" not in ctx


def test_the_cap_summary_is_derived_from_the_table(tmp_path: Path) -> None:
    """A second hand-written copy of the caps is one the drift test cannot see.

    Mutation (hardcode the old prose) -> BITES, because the derived summary must
    track _GH_DEFAULT_LIMITS rather than restate it.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS, _cap_summary

    summary = _cap_summary()
    for (group, _sub), cap in _GH_DEFAULT_LIMITS.items():
        assert group in summary, f"{group} missing from the derived cap summary"
        assert str(cap) in summary, f"cap {cap} missing from the derived cap summary"


def test_the_blind_block_never_prescribes_a_flag_it_cannot_know_exists(
    tmp_path: Path,
) -> None:
    """A branch that resolved NO target must prescribe NO mechanism.

    FIVE defects on this file share one root cause -- a remedy generalised past
    the family it was executed against -- and the last three were each inside the
    fix for the one before. MEASURED on gh 2.98.0:
      * `gh secret list --limit 100` -> `unknown flag: --limit` (so "pass --limit"
        is unfollowable there), AND
      * `gh api repos/cli/cli/issues --jq length` -> 30 with NO --limit flag at
        all (so "no --limit line means uncapped" certifies a capped read as
        complete -- worse than silence), AND
      * `gh search prs --help` advertises `--limit int ... (default 30)` and never
        mentions the 1000 ceiling (so "read its --help" hands the search defect
        straight back).
    The text now names the bounding shapes as WARNINGS and hands over the one
    check true of all of them. Mutation (restore any prescribed remedy) -> BITES.
    """
    ctx = _context(_run("gh secret list " + "#" * 60_000, tmp_path))
    assert "could not be read" in ctx
    # The three bounding shapes are named, so none is silently assumed away.
    assert "gh api" in ctx and "per_page" in ctx
    assert "gh search" in ctx and "1000" in ctx
    # The invariant that holds for all of them, and the one the reader gets.
    assert "reports a TOTAL" in ctx
    assert "do NOT take a short result as proof" in ctx
    # The three false sentences, pinned OUT by name.
    assert "no client-side limit is capping the read" not in ctx
    assert "every gh listing is capped" not in ctx


# --------------------------------------------------------------------------
# Round 4: fail LOUDLY. Three silences closed.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("group", ["pr", "issue", "run", "release", "repo", "gist", "cache"])
def test_ghs_builtin_ls_alias_is_not_a_silent_miss(group: str, tmp_path: Path) -> None:
    """gh ships `ls` as a BUILT-IN alias of `list`, carrying the same cap.

    Distinct from the documented one-word user-alias gap: `gh pr ls` is two
    words, so the walker resolves ("pr", "ls"), finds no row, and a command
    people actually type goes silent at its real cap.

    Mutation (drop the ls normalisation) -> BITES.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS

    cap = _GH_DEFAULT_LIMITS[(group, "list")]
    ctx = _context(_run(f"gh {group} ls", tmp_path, session=f"ls-{group}"))
    assert str(cap) in ctx


def test_ls_is_not_normalised_for_a_group_with_no_list_row(tmp_path: Path) -> None:
    """The normalisation is scoped to table groups, so it invents no coverage.

    `gh secret list` has no measured default and is deliberately silent; `gh
    secret ls` must not become a fabricated hit. Mutation (normalise `ls`
    unconditionally) -> still silent here, so this pins the SCOPE rather than
    the rewrite: it is the control for the test above.
    """
    assert _run("gh secret ls", tmp_path) is None


def test_listings_dropped_for_budget_are_announced(tmp_path: Path) -> None:
    """A deferral across commands is invisible WITHIN one, so say it out loud.

    Given caps for 9 of 13 listings with no cue that 4 went unmentioned, a reader
    can state a count from one of the four and never know a block existed.

    Mutation (drop the omission line) -> BITES.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS

    command = "; ".join(f"gh {g} {s}" for g, s in _GH_DEFAULT_LIMITS) + ' --search "unbalanced'
    ctx = _context(_run(command, tmp_path))
    assert "NOT described above" in ctx
    assert "output budget" in ctx
    # And it tells the reader the omission is recoverable, not a loss.
    assert "will describe it" in ctx


def test_a_dropped_listing_is_not_recorded_as_advised(tmp_path: Path) -> None:
    """The omission line's own claim has to be TRUE, not reassuring."""
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    from capped_read_advisory import _GH_DEFAULT_LIMITS

    targets = list(_GH_DEFAULT_LIMITS)
    command = "; ".join(f"gh {g} {s}" for g, s in targets) + ' --search "unbalanced'
    ctx = _context(_run(command, tmp_path))
    dropped = [(g, s) for g, s in targets if f"`gh {g} {s}`" not in ctx]
    assert dropped, "expected the budget to drop at least one listing"
    for group, sub in dropped:
        again = _context(_run(f"gh {group} {sub}", tmp_path))
        assert f"`gh {group} {sub}`" in again, f"{group} {sub} was announced but IS lost"


def test_a_crash_is_reported_on_stderr_not_swallowed(monkeypatch, capsys) -> None:
    """Fail open, but LOUDLY.

    A bare `contextlib.suppress(Exception)` made a crash indistinguishable from
    "no gh listing here" -- the hook reproducing, in its own error path, the
    silence it exists to break. stderr on an exit-0 PreToolUse hook is not shown
    to the model, so this costs the session nothing and still lands in the log.

    Driven through the REAL main() with a fault injected at its one entry point,
    because the obvious trigger does not work: malformed stdin does NOT raise --
    read_payload handles it and warns on its own, so a test built on that would
    have gone red for the wrong reason while proving nothing about this contract.

    Mutation (restore the bare suppress) -> BITES.
    """
    sys.path.insert(0, str(REPO / "scripts/hooks"))
    import capped_read_advisory as cra

    def _boom():
        raise RuntimeError("injected")

    monkeypatch.setattr(cra, "read_payload", _boom)
    rc = cra.main()
    captured = capsys.readouterr()
    assert rc == 0, "an advisory must never block, even on its own bug"
    assert "capped_read_advisory:" in captured.err
    assert "RuntimeError" in captured.err and "injected" in captured.err
    assert captured.out.strip() == "", "a crash must not emit a half-built advisory"
