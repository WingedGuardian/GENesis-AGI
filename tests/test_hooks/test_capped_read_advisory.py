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
